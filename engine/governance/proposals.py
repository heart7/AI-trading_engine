"""Policy proposals (spec §7.8, §20, INV-42). A proposal cannot go forward without a stress-battery PASS run for
the proposed policy's own hash; the principal then signs POLICY_ACTIVATE for that hash.

The baseline v10.4.0 PAPER activation is not a proposal (there is no prior policy), so it is governed by
coherence alone, where C15 is deferred in PAPER (decision 0001).
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from engine.policy.loader import Policy


class ProposalRefused(Exception):
    def __init__(self, reason: str, detail: str = ""):
        self.reason, self.detail = reason, detail
        super().__init__(f"{reason}: {detail}" if detail else reason)


@dataclass(frozen=True)
class Proposal:
    current_hash: str
    proposed: Policy
    stress_run_id: str


def propose(current: Policy, proposed: Policy, stress_run: Mapping[str, Any] | None) -> Proposal:
    if proposed.hash == current.hash:
        raise ProposalRefused("NO_CHANGE")
    if not stress_run:
        raise ProposalRefused("STRESS_BATTERY_MISSING", "run the stress battery on the proposed policy first")
    if stress_run.get("policy_hash") != proposed.hash:
        raise ProposalRefused("STRESS_BATTERY_STALE", "the battery run is for a different policy hash")
    if stress_run.get("verdict") != "PASS":
        failed = [s["id"] for s in stress_run.get("scenarios", []) if s.get("verdict") != "PASS"]
        raise ProposalRefused("STRESS_BATTERY_FAIL", ", ".join(failed))
    return Proposal(current.hash, proposed, stress_run["run_id"])
