"""Verdict engine, maturity state machine and promotion governor (spec §9.3, §9.4, §9.8, §10.2 L7).

- A step without a run_id is NOT RUN, never PASS (INV-28).
- Step 2 PASS requires the Sharpe CI lower bound > 0; a PASS on a non-positive bound is rejected (INV-28).
- Step 6 never gates SHADOW (INV-29); it only gates the regime layer's T0 -> T1 move.
- Evidence must come from offline replay; a live-only Sharpe is not a test of edge (INV-20).
- Promotions are capped per sleeve per quarter and programme-wide per year (INV-27).
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date

SHADOW_STEPS = frozenset({1, 2, 3, 4, 5, 7, 8})
MODES = ("PAPER", "SHADOW", "CANARY", "LIVE")


class VerdictRejected(Exception):
    pass


@dataclass(frozen=True)
class StepRecord:
    step: int
    sleeve: str
    verdict: str  # PASS | FAIL | NOT_RUN
    run_id: str | None
    metric: str
    value: float | None
    ci: tuple[float | None, float | None]
    source: str = "offline_replay"  # offline_replay | live
    details: Mapping = field(default_factory=dict)


def record_verdict(step: int, sleeve: str, verdict: str, run_id: str | None, metric: str, value: float | None,
                   ci: tuple[float | None, float | None], *, source: str = "offline_replay", details: Mapping | None = None) -> StepRecord:
    if verdict not in ("PASS", "FAIL", "NOT_RUN"):
        raise VerdictRejected(f"unknown verdict {verdict}")
    if run_id is None:
        verdict = "NOT_RUN"  # a step without a run_id renders NOT RUN, never PASS
    if verdict == "PASS":
        if source != "offline_replay":
            raise VerdictRejected("evidence of edge must come from offline replay, not a live horizon (INV-20)")
        if step == 2 and not (ci[0] is not None and ci[0] > 0):
            raise VerdictRejected(f"step 2 PASS needs CI lower bound > 0, got {ci[0]}")
    return StepRecord(step, sleeve, verdict, run_id, metric, value, ci, source, dict(details or {}))


def steps_passed(records: Iterable[StepRecord]) -> frozenset[int]:
    latest: dict[int, StepRecord] = {}
    for r in records:
        latest[r.step] = r
    return frozenset(s for s, r in latest.items() if r.verdict == "PASS")


def allowed_mode(records: Iterable[StepRecord], shadow_complete: bool = False, canary_complete: bool = False) -> str:
    """Highest mode the evidence allows. Step 6 is deliberately absent from every requirement (INV-29)."""
    passed = steps_passed(records)
    if not SHADOW_STEPS <= passed:
        return "PAPER"
    if not shadow_complete:
        return "SHADOW"
    if not canary_complete:
        return "CANARY"
    return "LIVE"


def regime_authority_allowed(records: Iterable[StepRecord]) -> str:
    return "T1" if 6 in steps_passed(records) else "T0"


@dataclass
class PromotionGovernor:
    per_sleeve_per_quarter: int
    programme_per_year: int
    history: list[tuple[date, str]] = field(default_factory=list)

    def promote(self, sleeve: str, on: date) -> None:
        q = (on.year, (on.month - 1) // 3)
        in_q = [s for d, s in self.history if (d.year, (d.month - 1) // 3) == q and s == sleeve]
        in_y = [s for d, s in self.history if d.year == on.year]
        if len(in_q) >= self.per_sleeve_per_quarter:
            raise VerdictRejected(f"{sleeve}: promotion cap per quarter reached")
        if len(in_y) >= self.programme_per_year:
            raise VerdictRejected("programme promotion cap per year reached")
        self.history.append((on, sleeve))
