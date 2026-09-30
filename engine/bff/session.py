"""The paper session the BFF projects (P5). PAPER only: no venue is contacted, no key is read.

A session is one fixture replay of the policy universe through the real decision code (signal, gates, sizing,
stops, loss ladder), plus the evidence plane on its trades (ledger, attribution, NAV dual path) and the regime
layer at T0. Everything it holds is FIXTURE (certified: false) and every figure it emits says so.

Playback safety (§16.7 Outcomes, §16.8.3): `mask(cursor)` returns a view where nothing timestamped after the
cursor exists. Masking happens here, server-side; the client never receives post-cursor data.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np

from engine.common.canonical import content_hash
from engine.common.incidents import IncidentLog
from engine.data.bars import to_arrays
from engine.data.fixtures import fixture_bars
from engine.evidence.drills import kill_verifier_drill, replay_evidence
from engine.policy.loader import load_policy, thaw
from engine.regime.ccmrm import RegimeLayer, confirm, raw_states
from engine.replay.paper import CostModel, ReplayConfig, ReplayResult, Series, replay
from engine.router.router import StrategyRouter

H4 = 4 * 3600
BPD = 6
WINDOW_BARS = 180  # 30 days of 4h bars
FIXTURE_START = datetime(2019, 1, 1, tzinfo=timezone.utc)
FIXTURE_MU_Q = 0.001  # ASSUMED (A-UI-FIXTURE-SESSION): the golden-replay expectancy, so the fixture trades
T_BAND_SIGMA = (0.8, 1.25)  # ASSUMED (A-T-BAND): T band = T recomputed with the EWMA vol estimate scaled by these
SESSION_CLOCK_OFFSET_S = 1000  # the session's "now" is this long after the last bar close (cycle complete)


def fixture_universe(policy: dict, bars: int) -> list[Series]:
    """FIXTURE series for the policy universe, in policy order (mandatory then default)."""
    names = list(policy["universe"]["A"]["mandatory"]) + list(policy["universe"]["A"]["default"])
    vols = {"BTC": 0.03, "ETH": 0.04, "XRP": 0.05, "SOL": 0.06}
    seeds = {"BTC": 100, "ETH": 101, "XRP": 102, "SOL": 103}
    out = []
    for base in names:
        a = to_arrays(fixture_bars(f"FIXTURE_{base}", FIXTURE_START, bars, seed=seeds.get(base, 200), vol_daily=vols.get(base, 0.05)))
        out.append(Series(f"FIXTURE_{base}", a["open_time"], a["o"], a["h"], a["l"], a["c"]))
    return out


def pair_name(instrument_id: str) -> str:
    return instrument_id.replace("FIXTURE_", "") + "/USD"


def base_of(instrument_id: str) -> str:
    return instrument_id.replace("FIXTURE_", "")


def t_band(c: np.ndarray, sig: dict[str, np.ndarray], policy: dict, idx: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """T recomputed at bars `idx` with sigma scaled by T_BAND_SIGMA; returns (lo, hi). B and Z are exact from closes."""
    s = policy["signal"]
    comps = []
    for mult in T_BAND_SIGMA:
        ms = []
        for m in s["momentum_lookbacks_m"]:
            days = 30 * m
            back = np.maximum(idx - days * BPD, 0)
            r = np.log(c[idx] / c[back])
            sd = sig["sigma_daily"][idx] * mult
            with np.errstate(invalid="ignore", divide="ignore"):
                ms.append(np.clip(r / (sd * math.sqrt(days)) / s["clip_t"], -1, 1))
        M = np.mean(ms, axis=0)
        comps.append((sig["B"][idx] + M + sig["Z"][idx]) / 3.0)
    lo, hi = np.minimum(*comps), np.maximum(*comps)
    T = sig["T"][idx]
    return np.minimum(lo, T), np.maximum(hi, T)


@dataclass
class PaperSession:
    policy: dict
    policy_hash: str
    policy_version: str
    series: list[Series]
    result: ReplayResult
    costs: CostModel
    regime_states: dict[str, list[str | None]]
    regime_claims: dict[str, dict]
    evidence: dict[str, Any]
    drill: dict[str, Any]
    incidents: IncidentLog
    harness: dict | None
    nav0: float
    now: datetime
    cursor: datetime | None = None
    stream_alive: bool = True
    stream_last_good: datetime | None = None
    reporter_log: list[dict] = field(default_factory=list)
    extra_activity: list[dict] = field(default_factory=list)  # test hook: injected late or extra events
    cycle_cache: dict = field(default_factory=dict)  # cycle index -> events (shared by masked views)

    # ---------- construction ----------
    @classmethod
    def build(cls, *, bars: int = 6 * 365 * 3, nav0: float = 30_000.0, harness_path: str | Path | None = None,
              policy_path: str | Path | None = None) -> PaperSession:
        pol = load_policy(policy_path)
        doc = thaw(pol.doc)
        series = fixture_universe(doc, bars)
        router = StrategyRouter(doc)
        costs = CostModel()
        res = replay(series, doc, router, ReplayConfig(nav0=nav0, mu_q_daily=FIXTURE_MU_Q, keep_intents_for_last_cycles=WINDOW_BARS),
                     costs)
        last_close = datetime.fromtimestamp(int(series[0].open_time[-1]) + H4, tz=timezone.utc)
        now = last_close + timedelta(seconds=SESSION_CLOCK_OFFSET_S)
        layer = RegimeLayer(doc, pol.hash)
        states, claims = {}, {}
        for s in series:
            states[s.instrument_id] = confirm(raw_states(s.c), doc["regime"]["confirm_bars"])
            claims[s.instrument_id] = layer.claim(s.instrument_id, s.c, len(s.c) - 1, last_close, now=now)
        ev = replay_evidence(res.trades, {s.instrument_id: s for s in series}, nav0=nav0)
        harness = None
        hp = Path(harness_path) if harness_path else None
        if hp is not None and hp.exists():
            import json
            harness = json.loads(hp.read_text())
        return cls(doc, pol.hash, pol.version, series, res, costs, states, claims, ev, kill_verifier_drill(), IncidentLog(),
                   harness, nav0, now, stream_last_good=now)

    # ---------- clock and masking ----------
    @property
    def clock(self) -> datetime:
        return self.cursor or self.now

    def mask(self, cursor: datetime | None) -> PaperSession:
        """A view with nothing after `cursor`. Used by every playback route (server-side masking)."""
        if cursor is None or cursor >= self.now:
            return self
        return replace(self, cursor=cursor)

    def last_index(self) -> int:
        """Index of the last bar whose close is at or before the clock."""
        closes = self.series[0].open_time + H4
        return int(np.searchsorted(closes, int(self.clock.timestamp()), side="right") - 1)

    def bar_close(self, i: int) -> datetime:
        return datetime.fromtimestamp(int(self.series[0].open_time[i]) + H4, tz=timezone.utc)

    def by_id(self, instrument_id: str) -> Series:
        for s in self.series:
            if s.instrument_id == instrument_id or base_of(s.instrument_id) == instrument_id.split("/")[0].upper():
                return s
        raise KeyError(instrument_id)

    def trades_visible(self) -> tuple[list, list]:
        """(closed, open) trades as seen at the clock. END_OF_REPLAY trades and trades exiting after the cursor are open."""
        cut = int(self.clock.timestamp())
        closed, open_ = [], []
        for t in self.result.trades:
            if t.entry_time > cut:
                continue
            if t.exit_reason == "END_OF_REPLAY" or t.exit_time > cut:
                open_.append(t)
            else:
                closed.append(t)
        return closed, open_

    def intents_visible(self) -> list[dict]:
        cut = self.clock.isoformat()
        return [x for x in self.result.intents_tail if x["bar_close"] <= cut]

    def equity_visible(self) -> tuple[np.ndarray, np.ndarray]:
        cut = int(self.clock.timestamp())
        m = self.result.equity_time <= cut
        return self.result.equity_time[m], self.result.equity[m]

    def data_hash(self) -> str:
        return content_hash([[s.instrument_id, int(s.open_time[0]), len(s.c), repr(float(s.c[-1]))] for s in self.series])

    # ---------- stream (activity WS/SSE) ----------
    def kill_stream(self) -> None:
        self.stream_alive = False

    def restore_stream(self) -> None:
        self.stream_alive = True
        self.stream_last_good = self.now
