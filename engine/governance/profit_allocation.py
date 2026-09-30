"""Profit allocation (spec §12.6, §8.4).

The reinvest percentage (0-100%) is a policy setting. Changing it is a proposal that takes effect only with a
hardware-signed PROFIT_ALLOCATION approval over the exact change. Sweeping profit off-venue is a transfer_intent
that a human executes with keys the engine never holds (INV-02); the engine only proposes the amount.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from engine.common.canonical import content_hash
from engine.evidence.collateral import TransferIntent


class AllocationRefused(Exception):
    def __init__(self, reason: str, detail: str = ""):
        self.reason, self.detail = reason, detail
        super().__init__(f"{reason}: {detail}" if detail else reason)


@dataclass(frozen=True)
class AllocationProposal:
    current_pct: float
    proposed_pct: float
    policy_hash: str

    @property
    def subject_hash(self) -> str:
        return content_hash({"kind": "profit_allocation", "from": self.current_pct, "to": self.proposed_pct,
                             "policy_hash": self.policy_hash})


def propose(current_pct: float, proposed_pct: float, policy_hash: str) -> AllocationProposal:
    if not 0 <= proposed_pct <= 100:
        raise AllocationRefused("OUT_OF_RANGE", "reinvest percentage must be 0-100")
    if proposed_pct == current_pct:
        raise AllocationRefused("NO_CHANGE")
    return AllocationProposal(current_pct, proposed_pct, policy_hash)


def apply(p: AllocationProposal, approval: Mapping[str, str] | None, verify: Callable[[Mapping[str, str], str], Any]) -> float:
    """Returns the new reinvest percentage once the approval verifies (verify raises on refusal)."""
    if approval is None:
        raise AllocationRefused("APPROVAL_REQUIRED", "a hardware-signed PROFIT_ALLOCATION approval")
    verify(approval, p.subject_hash)
    return p.proposed_pct


def sweep_intent(period_net_profit_usd: float, tax_reserve_usd: float, reinvest_pct: float, *, venue: str,
                 period: str) -> TransferIntent | None:
    """Profit left after the tax reserve, times (1 - reinvest), proposed as a human-executed transfer off-venue."""
    distributable = period_net_profit_usd - tax_reserve_usd
    amount = round(distributable * (1 - reinvest_pct / 100), 2)
    if amount <= 0:
        return None
    return TransferIntent(venue, "off-exchange-reserve", "USD", amount, f"profit sweep {period} at {reinvest_pct:g}% reinvest")
