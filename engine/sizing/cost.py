"""Strategy A cost gate (spec §5.4, INV-06). It cannot be bypassed: there is no flag to skip it."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True)
class CostInput:
    value: float  # fraction of notional
    as_of: datetime
    ttl: timedelta

    def fresh(self, now: datetime) -> bool:
        return now - self.as_of <= self.ttl


@dataclass(frozen=True)
class CostGateResult:
    passed: bool
    gate: str | None  # COST_INPUT_STALE | COST_R_EXCEEDED | None
    d: float
    c: float
    cost_R: float | None


def cost_gate(*, k_stop: float, atr_daily: float, entry_px: float, fee_roundtrip: CostInput, spread_at_size: CostInput,
              slippage_q75: CostInput, cost_R_max: float, now: datetime) -> CostGateResult:
    d = k_stop * atr_daily / entry_px
    parts = (fee_roundtrip, spread_at_size, slippage_q75)
    c = sum(x.value for x in parts)
    if not all(x.fresh(now) for x in parts):
        return CostGateResult(False, "COST_INPUT_STALE", d, c, None)
    if d <= 0:
        return CostGateResult(False, "COST_R_EXCEEDED", d, c, None)
    cost_r = c / d
    return CostGateResult(cost_r <= cost_R_max, None if cost_r <= cost_R_max else "COST_R_EXCEEDED", d, c, cost_r)
