"""Stress battery S1-S10 (spec §7.8). Run on every policy proposal (INV-42) and monthly on the live book.

The battery stresses the largest book the policy permits at a tier (every slot filled, gross at the venue cap
less the operating buffer, operating cash on the venue), unless a live book is given. Replay scenarios need
certified history (blocked in P1), so S1, S2, S3, S5 and S6 run here as synthetic proxies with the moves the
spec names; each is labelled SYNTHETIC_PROXY and must be re-run as a replay once history is certified.

PASS rule: loss <= hard DD budget, and FLATTEN completes within the time-to-flatten ceiling (§7.5).
Spot Strategy A posts no margin, so INV-14 headroom is reported as not applicable.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from engine.common.canonical import content_hash

# ASSUMED book shape for the policy-maximal stress book (A-STRESS-BOOK).
ASSUMED_STOP_DISTANCE = 0.08  # k_stop 2 x daily ATR ~4%
ASSUMED_STRESSED_VOL_USD_PER_MIN = {"BTC": 2.0e6, "ETH": 1.0e6, "XRP": 3.0e5, "SOL": 3.0e5}


@dataclass(frozen=True)
class Position:
    instrument: str
    venue: str
    notional: float  # share of NAV
    stop_distance: float  # fraction below entry


@dataclass(frozen=True)
class Book:
    nav_usd: float
    positions: tuple[Position, ...]
    venue_cash: Mapping[str, float]  # share of NAV held as cash on each venue
    stables: Mapping[str, float] = field(default_factory=dict)  # issuer -> share of NAV
    offvenue: float = 0.0


def policy_max_book(policy: Mapping[str, Any], tier: str, instruments: tuple[str, ...] = ("BTC", "XRP", "ETH", "SOL"),
                    venue: str = "kraken-spot") -> Book:
    t = policy["tiers"][tier]
    n = int(t["n_max"]["A"])
    r = t["r"]["A"]
    cap, buf = policy["risk"]["venue_exposure_max"], policy["collateral"]["spot_operating_buffer"]
    per = min(r / ASSUMED_STOP_DISTANCE, policy["sizing"]["per_pair_notional_max"])
    gross = min(per * n, cap - buf, 1.0)  # spot gross <= 1 x NAV (§5.5)
    pos = tuple(Position(instruments[i % len(instruments)], venue, gross / n, ASSUMED_STOP_DISTANCE) for i in range(n))
    return Book(float(t["nav_up"]), pos, {venue: buf}, {}, 1.0 - gross - buf)


def _stopped(p: Position, beyond: float) -> float:
    """Loss share of NAV when the stop fills `beyond` (fraction of price) below its trigger."""
    fill = (1 - p.stop_distance) * (1 - beyond)
    return p.notional * (1 - fill)


def _moved(p: Position, move: float, beyond: float) -> float:
    """Price falls by `move`; the stop fires if the move reaches it, else the position is marked down."""
    return _stopped(p, beyond) if move >= p.stop_distance else p.notional * move


def _alt(i: str) -> bool:
    return i not in ("BTC",)


SCENARIOS: dict[str, tuple[str, str]] = {
    "S1": ("COVID crash Mar 2020: -50% in 2 days, venue outage", "SYNTHETIC_PROXY"),
    "S2": ("May 2021: -30% intraday, API degradation", "SYNTHETIC_PROXY"),
    "S3": ("LUNA/UST May 2022: algorithmic stablecoin to ~0, market -40%", "SYNTHETIC_PROXY"),
    "S4": ("FTX Nov 2022: 100% loss of all assets at the largest-exposure venue", "SYNTHETIC"),
    "S5": ("USDC Mar 2023: depeg to 0.88", "SYNTHETIC_PROXY"),
    "S6": ("Oct 2025 alt cascade: alts -30% to -70%, BTC -15%", "SYNTHETIC_PROXY"),
    "S7": ("All instruments -40% in 24h, stops filled 25% beyond trigger", "SYNTHETIC"),
    "S8": ("Execution venue API down 6h with open positions", "SYNTHETIC"),
    "S9": ("Funding +/-0.30%/8h for 10 days", "SYNTHETIC"),
    "S10": ("Single coin +/-40% gap on a legal ruling", "SYNTHETIC"),
}


def scenario_loss(sid: str, b: Book) -> float:
    ps = b.positions
    if sid == "S1":
        return sum(_moved(p, 0.50, 0.10) for p in ps)
    if sid == "S2":
        return sum(_moved(p, 0.30, 0.05) for p in ps)
    if sid == "S3":
        return sum(_moved(p, 0.40, 0.05) for p in ps) + sum(v * 0.95 for k, v in b.stables.items() if k == "algorithmic")
    if sid == "S4":
        by_venue: dict[str, float] = dict(b.venue_cash)
        for p in ps:
            by_venue[p.venue] = by_venue.get(p.venue, 0.0) + p.notional
        return max(by_venue.values(), default=0.0)
    if sid == "S5":
        return sum(v * 0.12 for v in b.stables.values())
    if sid == "S6":
        return sum(_moved(p, 0.50 if _alt(p.instrument) else 0.15, 0.15 if _alt(p.instrument) else 0.03) for p in ps)
    if sid == "S7":
        return sum(_moved(p, 0.40, 0.25) for p in ps)
    if sid == "S8":
        return sum(_moved(p, 0.20, 0.02) for p in ps)  # venue-resident stops still fire; no ratchets for 6h
    if sid == "S9":
        return 0.0  # spot A pays no funding; B books are PAPER-only (P4)
    if sid == "S10":
        return max((p.notional * 0.40 for p in ps), default=0.0)  # gap straight through the stop
    raise KeyError(sid)


def time_to_flatten_min(b: Book, participation: float = 0.10, stress_depth: float = 0.3) -> float:
    per_inst: dict[str, float] = {}
    for p in b.positions:
        per_inst[p.instrument] = per_inst.get(p.instrument, 0.0) + p.notional * b.nav_usd
    worst = 0.0
    for inst, usd in per_inst.items():
        vol = ASSUMED_STRESSED_VOL_USD_PER_MIN.get(inst, 1.0e5) * stress_depth * participation
        worst = max(worst, usd / vol)
    return worst


def run_battery(policy: Mapping[str, Any], policy_hash: str, *, tier: str = "T2", book: Book | None = None,
                sleeve: str = "A", now: datetime | None = None) -> dict[str, Any]:
    b = book or policy_max_book(policy, tier)
    hard = policy["risk"]["ladder"]["dd"]["terminate"]
    rungs = policy["risk"]["ladder"]["dd"]
    ceiling = policy["risk"]["ttf_ceiling_min"][sleeve]
    ttf = time_to_flatten_min(b)
    rows = []
    for sid, (name, kind) in SCENARIOS.items():
        loss = scenario_loss(sid, b)
        hit = [k for k, v in rungs.items() if loss >= v]
        ok = loss <= hard + 1e-12 and ttf <= ceiling
        rows.append({"id": sid, "name": name, "kind": kind, "loss": round(loss, 6), "rungs_hit": hit,
                     "margin_headroom": "n/a (spot, no posted margin)", "liquidations": 0,
                     "time_to_flatten_min": round(ttf, 2), "verdict": "PASS" if ok else "FAIL"})
    body = {"policy_hash": policy_hash, "tier": tier, "book": asdict(b), "hard_dd": hard, "ttf_ceiling_min": ceiling,
            "scenarios": rows, "verdict": "PASS" if all(r["verdict"] == "PASS" for r in rows) else "FAIL",
            "at": (now or datetime.now(timezone.utc)).isoformat()}
    body["run_id"] = "stress-" + content_hash(body)[:16]
    return body
