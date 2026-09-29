"""Mode ladder and live ramp (spec §9.8, §9.3 step 9, §3.3 G2).

    PAPER -> SHADOW (>= 90 d) -> CANARY (<= 5% of tier capital, >= 60 d) -> LIVE 25% -> 50% -> 100% (>= 30 d each)

Rules enforced here:
- Promotion is one step at a time and needs a verified hardware-signed PROMOTE approval for this exact move.
- PAPER -> SHADOW needs §9.3 steps 1-5, 7, 8 PASS. Step 6 is never required (INV-29).
- Every later promotion needs the current step's record: dwell met, recon >= recon_min, cost divergence below
  divergence_block, zero H1/D1 incidents in the step, and OBSERVED evidence. A FIXTURE record never promotes.
- `review()` runs on every step record: any failing gate demotes exactly one step (and never below PAPER).
- CANARY and LIVE rungs cap the capital the engine may deploy; PAPER and SHADOW deploy none.
The ladder never places orders; it answers "which mode may run and with how much capital".
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from engine.common.canonical import content_hash
from research.harness.verdicts import MODES, StepRecord, allowed_mode

RUNGS = ("PAPER", "SHADOW", "CANARY", "LIVE_25", "LIVE_50", "LIVE_100")
LIVE_RUNG_DAYS = 30
LIVE_FRACTION = {"LIVE_25": 0.25, "LIVE_50": 0.50, "LIVE_100": 1.0}


class ModeRefused(Exception):
    def __init__(self, reason: str, detail: str = ""):
        self.reason, self.detail = reason, detail
        super().__init__(f"{reason}: {detail}" if detail else reason)


def base_mode(rung: str) -> str:
    """The engine mode a rung runs in (the four modes of §9.8)."""
    m = "LIVE" if rung.startswith("LIVE") else rung
    assert m in MODES
    return m


def dwell_days(rung: str, policy: Mapping[str, Any]) -> int:
    v = policy["validation"]
    return {"PAPER": 0, "SHADOW": int(v["shadow_days"]), "CANARY": int(v["canary"]["days"]),
            "LIVE_25": LIVE_RUNG_DAYS, "LIVE_50": LIVE_RUNG_DAYS, "LIVE_100": 0}[rung]


def capital_fraction(rung: str, policy: Mapping[str, Any]) -> float:
    """Share of tier capital the rung may deploy."""
    if rung in ("PAPER", "SHADOW"):
        return 0.0
    if rung == "CANARY":
        return float(policy["validation"]["canary"]["capital_max"])
    return LIVE_FRACTION[rung]


@dataclass(frozen=True)
class StepEvidence:
    """What one rung's record shows (built from the shadow journal by engine.shadow.metrics)."""
    rung: str
    days: int
    recon: float
    cost_divergence: float | None
    h1_d1_incidents: int
    evidence_class: str  # OBSERVED | FIXTURE

    def gates(self, policy: Mapping[str, Any]) -> list[dict[str, Any]]:
        need = dwell_days(self.rung, policy)
        block = float(policy["cost"]["divergence_block"])
        rmin = float(policy["validation"]["recon_min"])
        return [
            {"gate": "DWELL", "passed": self.days >= need, "detail": f"{self.days} of {need} days"},
            {"gate": "RECON", "passed": self.recon >= rmin, "detail": f"recon {self.recon:.4%} vs {rmin:.1%}"},
            {"gate": "COST_DIVERGENCE", "passed": self.cost_divergence is not None and self.cost_divergence < block,
             "detail": "no priced intents yet" if self.cost_divergence is None
             else f"divergence {self.cost_divergence:.1%} vs {block:.0%}"},
            {"gate": "H1_D1", "passed": self.h1_d1_incidents == 0, "detail": f"{self.h1_d1_incidents} H1/D1 incidents"},
            {"gate": "EVIDENCE_CLASS", "passed": self.evidence_class == "OBSERVED",
             "detail": f"record class {self.evidence_class}"},
        ]


def promotion_subject(frm: str, to: str, policy_hash: str, evidence: StepEvidence | None) -> str:
    """What the principal signs for a promotion: the move, the policy and the step record it rests on."""
    ev = None if evidence is None else {"rung": evidence.rung, "days": evidence.days, "recon": evidence.recon,
                                         "cost_divergence": evidence.cost_divergence,
                                         "h1_d1": evidence.h1_d1_incidents, "class": evidence.evidence_class}
    return content_hash({"from": frm, "to": to, "policy_hash": policy_hash, "evidence": ev})


Verifier = Callable[[Mapping[str, str], str], Any]  # (approval, expected_subject) -> raises on refusal


@dataclass
class ModeLadder:
    policy: Mapping[str, Any]
    policy_hash: str
    rung: str = "PAPER"
    since: date | None = None
    history: list[dict[str, Any]] = field(default_factory=list)

    @property
    def mode(self) -> str:
        return base_mode(self.rung)

    @property
    def capital_fraction(self) -> float:
        return capital_fraction(self.rung, self.policy)

    def _move(self, to: str, on: date, why: str) -> dict[str, Any]:
        ev = {"from": self.rung, "to": to, "on": on.isoformat(), "reason": why}
        self.history.append(ev)
        self.rung, self.since = to, on
        return ev

    def promote(self, to: str, *, on: date, records: Iterable[StepRecord], evidence: StepEvidence | None,
                approval: Mapping[str, str] | None, verify: Verifier) -> dict[str, Any]:
        if to not in RUNGS:
            raise ModeRefused("UNKNOWN_RUNG", to)
        i = RUNGS.index(self.rung)
        if RUNGS.index(to) != i + 1:
            raise ModeRefused("ONE_STEP_AT_A_TIME", f"{self.rung} -> {to}")
        if self.rung == "PAPER":
            if allowed_mode(records) == "PAPER":  # steps 1-5, 7, 8; step 6 is not consulted (INV-29)
                raise ModeRefused("VALIDATION_STEPS_MISSING", "SHADOW needs §9.3 steps 1-5, 7, 8 PASS")
        else:
            if evidence is None or evidence.rung != self.rung:
                raise ModeRefused("STEP_RECORD_MISSING", f"no record for {self.rung}")
            failed = [g for g in evidence.gates(self.policy) if not g["passed"]]
            if failed:
                raise ModeRefused("STEP_GATES_FAIL", "; ".join(f"{g['gate']} ({g['detail']})" for g in failed))
        if approval is None:
            raise ModeRefused("APPROVAL_REQUIRED", "a hardware-signed PROMOTE approval for this move")
        verify(approval, promotion_subject(self.rung, to, self.policy_hash, evidence))
        return self._move(to, on, "promoted with signed approval")

    def review(self, evidence: StepEvidence, *, on: date) -> dict[str, Any] | None:
        """Run on each step record. A failing gate (other than dwell) demotes one rung."""
        if self.rung == "PAPER" or evidence.rung != self.rung:
            return None
        failed = [g for g in evidence.gates(self.policy) if not g["passed"] and g["gate"] not in ("DWELL", "EVIDENCE_CLASS")
                  and not (g["gate"] == "COST_DIVERGENCE" and evidence.cost_divergence is None)]
        if not failed:
            return None
        to = RUNGS[RUNGS.index(self.rung) - 1]
        return self._move(to, on, "demoted: " + ", ".join(g["gate"] for g in failed))
