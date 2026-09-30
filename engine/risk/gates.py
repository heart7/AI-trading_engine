"""Gate ladder (spec §7.1) and the exit/add rules (§5.3, §5.7).

Every evaluation returns the full ladder; the first failing gate in ladder order is the binding gate.
The persisted ladder is what the UI shows (never a recomputed one). Exits never pass through the
evidence, cost or risk-budget gates (INV-07).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

LADDER_ORDER = [
    "ENGINE_STATE", "TIER_NOT_ACTIVE", "DATA_STALE", "DATA_UNCERTIFIED", "INSUFFICIENT_HISTORY", "NOT_ADMISSIBLE",
    "NO_BREAKOUT", "T_BELOW_ENTRY", "EVIDENCE_NOT_ON_FILE", "COST_INPUT_STALE", "COST_R_EXCEEDED", "RISK_BUDGET",
    "MARGIN_INVARIANT", "VENUE_UNHEALTHY", "PROTECTION_UNVERIFIED", "ADL_REDUCE", "CIRCUIT_BREAKER",
]


@dataclass
class EntryContext:
    engine_running: bool
    sleeve_allowed: bool
    data_fresh: bool
    data_certified: bool
    signal_abstain: str | None
    admissible: bool
    B: float | None
    T: float | None
    T_entry: float
    side: int
    evidence_on_file: bool
    cost_gate: str | None  # None pass, else COST_INPUT_STALE / COST_R_EXCEEDED
    cost_R: float | None
    cost_R_max: float
    risk_budget_ok: bool
    margin_ok: bool = True
    venue_healthy: bool = True
    protection_verified: bool = True
    adl_ok: bool = True
    breaker_clear: bool = True
    extra: dict = field(default_factory=dict)


def evaluate_entry(x: EntryContext) -> tuple[list[dict], str | None]:
    s = x.side
    breakout = x.B is not None and (x.B > 0 if s > 0 else x.B < 0)
    t_ok = x.T is not None and (x.T >= x.T_entry if s > 0 else x.T <= -x.T_entry)
    results = {
        "ENGINE_STATE": x.engine_running,
        "TIER_NOT_ACTIVE": x.sleeve_allowed,
        "DATA_STALE": x.data_fresh,
        "DATA_UNCERTIFIED": x.data_certified,
        "INSUFFICIENT_HISTORY": x.signal_abstain != "INSUFFICIENT_HISTORY",
        "NOT_ADMISSIBLE": x.admissible,
        "NO_BREAKOUT": breakout,
        "T_BELOW_ENTRY": t_ok,
        "EVIDENCE_NOT_ON_FILE": x.evidence_on_file,
        "COST_INPUT_STALE": x.cost_gate != "COST_INPUT_STALE",
        "COST_R_EXCEEDED": x.cost_gate != "COST_R_EXCEEDED",
        "RISK_BUDGET": x.risk_budget_ok,
        "MARGIN_INVARIANT": x.margin_ok,
        "VENUE_UNHEALTHY": x.venue_healthy,
        "PROTECTION_UNVERIFIED": x.protection_verified,
        "ADL_REDUCE": x.adl_ok,
        "CIRCUIT_BREAKER": x.breaker_clear,
    }
    values = {"NO_BREAKOUT": (x.B, 0.0), "T_BELOW_ENTRY": (x.T, x.T_entry), "COST_R_EXCEEDED": (x.cost_R, x.cost_R_max)}
    ladder = []
    for g in LADDER_ORDER:
        v, lim = values.get(g, (None, None))
        ladder.append({"gate": g, "passed": bool(results[g]), "value": v, "limit": lim})
    binding = next((row["gate"] for row in ladder if not row["passed"]), None)
    return ladder, binding


@dataclass(frozen=True)
class ExitCheck:
    exit: bool
    reason: str | None


def evaluate_exit(*, side: int, T: float | None, stop_hit: bool, initial_hit: bool, time_stop_state: str,
                  risk_instruction: bool) -> ExitCheck:
    """First of: T crosses 0 against the position, trailing stop, initial stop, time stop, risk-kernel instruction.
    No evidence, cost or budget input exists in this signature by design (INV-07)."""
    if T is not None and (T <= 0 if side > 0 else T >= 0):
        return ExitCheck(True, "T_CROSSED_ZERO")
    if stop_hit and not initial_hit:
        return ExitCheck(True, "TRAILING_STOP")
    if initial_hit:
        return ExitCheck(True, "INITIAL_STOP")
    if time_stop_state == "REDUCE_ONLY":
        return ExitCheck(True, "TIME_STOP")
    if risk_instruction:
        return ExitCheck(True, "RISK_KERNEL")
    return ExitCheck(False, None)


class AddRejected(Exception):
    pass


def check_add(*, open_R: float, adds_so_far: int, gates_pass: bool, new_combined_risk_usd: float, r_tier: float,
              nav: float) -> None:
    """Spec §5.7 / INV-08. Combined risk is measured from the current trailing stop by the caller."""
    if open_R < 0:
        raise AddRejected("ADD_ON_LOSER")
    if open_R < 1.0:
        raise AddRejected("ADD_BELOW_1R")
    if adds_so_far >= 1:
        raise AddRejected("ADD_LIMIT")
    if not gates_pass:
        raise AddRejected("GATES_FAIL")
    if new_combined_risk_usd > r_tier * nav + 1e-9:
        raise AddRejected("ADD_RISK_EXCEEDS_R")


def cluster_ok(open_risk_usd: list[float], new_risk_usd: float, open_risk_cap: float, nav: float) -> bool:
    return math.fsum(open_risk_usd) + new_risk_usd <= open_risk_cap * nav + 1e-9
