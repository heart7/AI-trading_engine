"""SHADOW runner (spec §9.3 step 9, §9.7, §9.8, §13.4, §19 P6).

SHADOW means live data and no orders. At each 4h bar close the runner:
1. checks the policy is the one it started with (live parameters are frozen during SHADOW; a change without a
   new signed policy opens H1 PARAMS_NOT_FROZEN and the cycle abstains);
2. checks every instrument's bar for this close is present and certified (else D1 BAR_MISSING, that instrument
   abstains);
3. runs the same decision code as PAPER and replay (engine.replay.paper.replay) over the certified history, in
   the ladder's mode, and keeps this cycle's intents: what the engine *would* have done;
4. recomputes T through the independent verifier and records agreement (a mismatch opens D1 RECON_BREAK);
5. prices each would-be entry twice, with the cost model and with the order book observed at the decision
   (A-SHADOW-COST-OBS), so cost divergence can be measured before any real fill exists.
Each cycle is one record in an append-only, hash-chained journal. Nothing here holds a key or can reach a venue's
order endpoint: the runner has no OMS and no adapter.
"""
from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from engine.data.store import AppendOnlyLog
from engine.evidence.verifier import independent_signal
from engine.replay.paper import CostModel, ReplayConfig, Series, replay
from engine.router.router import StrategyRouter
from research.harness.verdicts import StepRecord, allowed_mode

H4 = 4 * 3600
RECON_TOL = 1e-9
IMPACT_AT_DEPTH = 0.005  # ASSUMED (A-SHADOW-COST-OBS): taking the whole 50 bp depth costs 50 bp, linear below it


@dataclass(frozen=True)
class Quote:
    """Top of book and depth observed at the decision, from a public (keyless) order-book endpoint."""
    bid: float
    ask: float
    depth_50bp_usd: float
    observed_at: datetime

    @property
    def half_spread(self) -> float:
        mid = (self.bid + self.ask) / 2
        return (self.ask - self.bid) / 2 / mid

    def cost(self, notional: float) -> float:
        """Observed entry cost as a fraction of notional: half-spread plus impact through the book."""
        impact = IMPACT_AT_DEPTH * min(1.0, notional / self.depth_50bp_usd) if self.depth_50bp_usd > 0 else IMPACT_AT_DEPTH
        return self.half_spread + impact


class ShadowJournal:
    """Append-only, hash-chained record of shadow cycles (one JSON line per 4h cycle)."""

    def __init__(self, path: Path):
        self.log = AppendOnlyLog(Path(path))

    def append(self, rec: Mapping[str, Any]) -> str:
        return self.log.append(dict(rec))

    def records(self) -> list[dict[str, Any]]:
        return self.log.records()

    def verify(self) -> int:
        return self.log.verify()


def align(series: Sequence[Series]) -> list[Series]:
    """Common contiguous grid for replay: same last bar, first bar at the latest common 00:00 UTC."""
    from dataclasses import replace

    start = max(int(s.open_time[0]) for s in series)
    start += (-start) % 86400
    out = []
    for s in series:
        k = int(np.searchsorted(s.open_time, start))
        out.append(replace(s, open_time=s.open_time[k:], o=s.o[k:], h=s.h[k:], l=s.l[k:], c=s.c[k:]))
    return out


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _num(x: Any) -> float | None:
    if x is None:
        return None
    x = float(x)
    return None if math.isnan(x) else x


@dataclass
class ShadowRunner:
    policy: Mapping[str, Any]
    policy_hash: str  # frozen at start; SHADOW never changes live parameters
    journal: ShadowJournal
    costs: CostModel = CostModel()
    evidence_class: str = "OBSERVED"  # FIXTURE for drills; a FIXTURE journal never counts toward a gate
    nav0: float = 30_000.0
    mu_q_daily: float | Mapping | None = None  # from the harness; None sizes to zero (§5.5)

    def cycle(self, series: Sequence[Series], *, bar_close: datetime, now: datetime, loaded_policy_hash: str,
              rung: str, records: Iterable[StepRecord] = (), quotes: Mapping[str, Quote] | None = None) -> dict[str, Any]:
        from engine.modes.ladder import base_mode

        quotes = quotes or {}
        mode = base_mode(rung)
        rec: dict[str, Any] = {"bar_close": bar_close.isoformat(), "observed_at": now.isoformat(), "rung": rung,
                               "mode": mode, "policy_hash": self.policy_hash, "class": self.evidence_class,
                               "instruments": [], "incidents": []}
        if loaded_policy_hash != self.policy_hash:
            rec["incidents"].append({"code": "PARAMS_NOT_FROZEN", "severity": "H1",
                                     "detail": "policy changed during SHADOW without a new signed policy"})
            rec["abstained"] = "PARAMS_NOT_FROZEN"
            self.journal.append(rec)
            return rec

        want = int(bar_close.timestamp()) - H4  # open_time of the bar that just closed
        ready, missing = [], []
        for s in series:
            ok = len(s.open_time) > 0 and int(s.open_time[-1]) == want and (s.certified or self.evidence_class == "FIXTURE")
            (ready if ok else missing).append(s)
        for s in missing:
            rec["incidents"].append({"code": "BAR_MISSING", "severity": "D1", "instrument_id": s.instrument_id,
                                     "detail": f"no certified bar for {bar_close.isoformat()}"})
            rec["instruments"].append({"instrument_id": s.instrument_id, "outcome": "ABSTAINED",
                                       "binding_gate": "DATA_STALE", "recon_ok": None})
        if not ready:
            self.journal.append(rec)
            return rec

        ready = align(ready)
        evidence = mode == "PAPER" or allowed_mode(records) != "PAPER"  # PAPER needs no evidence; SHADOW+ do
        res = replay(ready, self.policy, StrategyRouter(self.policy),
                     ReplayConfig(nav0=self.nav0, mode=mode, mu_q_daily=self.mu_q_daily, evidence_on_file=evidence,
                                  keep_intents_for_last_cycles=1), self.costs)
        intents = {x["instrument_id"]: x for x in res.intents_tail if x["bar_close"] == _iso(want + H4)}
        sig = self.policy["signal"]
        lam = self.policy["sizing"]["ewma_lambda"]
        for s in ready:
            i = len(s.c) - 1
            t_eng = _num(res.signals[s.instrument_id]["T"][i])
            t_ver = independent_signal(list(s.h), list(s.l), list(s.c), i, sig, lam)
            ok = (t_eng is None and t_ver is None) or (t_eng is not None and t_ver is not None and abs(t_eng - t_ver) <= RECON_TOL)
            it = intents.get(s.instrument_id, {})
            row: dict[str, Any] = {"instrument_id": s.instrument_id, "T_engine": t_eng, "T_verifier": t_ver,
                                   "recon_ok": ok, "outcome": it.get("outcome", "HOLDING"),
                                   "binding_gate": it.get("binding_gate"), "notional": it.get("notional")}
            if not ok:
                rec["incidents"].append({"code": "RECON_BREAK", "severity": "D1", "instrument_id": s.instrument_id,
                                         "detail": f"T engine {t_eng} vs verifier {t_ver}"})
            q = quotes.get(s.instrument_id)
            if it.get("outcome") == "ENTERED" and it.get("notional"):
                row["cost_predicted"] = self.costs.half_spread + self.costs.slippage_q75
                row["cost_observed"] = q.cost(float(it["notional"])) if q is not None else None
            rec["instruments"].append(row)
        rec["equity_last"] = float(res.equity[-1]) if len(res.equity) else self.nav0
        self.journal.append(rec)
        return rec
