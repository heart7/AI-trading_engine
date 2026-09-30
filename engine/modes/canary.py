"""Deployment guard for CANARY and LIVE rungs, and the on-call rule (spec §9.8, §13.8, §21 item 13).

- PAPER and SHADOW send no orders at all.
- From CANARY up, a named human must be on call for the day. With nobody on call (a holiday window, a gap in the
  rota) the engine is in STOP: entries blocked, venue-resident stops keep protecting open positions.
- Deployed capital per rung is capped: CANARY at validation.canary.capital_max of tier capital (5%), then 25%, 50%
  and 100% on the LIVE ramp. The cap reads the ladder's rung; nothing here can raise it.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from engine.modes.ladder import base_mode, capital_fraction


@dataclass(frozen=True)
class Shift:
    person: str
    start: date
    end: date  # inclusive


@dataclass
class OnCallRota:
    """Governance setting with dates (§13.8)."""
    shifts: Sequence[Shift] = ()
    stop_windows: Sequence[tuple[date, date]] = field(default_factory=list)  # declared holidays: engine in STOP

    def on_call(self, day: date) -> str | None:
        if any(a <= day <= b for a, b in self.stop_windows):
            return None
        return next((s.person for s in self.shifts if s.start <= day <= s.end), None)

    def uncovered(self, start: date, days: int) -> list[date]:
        return [start + timedelta(days=k) for k in range(days) if self.on_call(start + timedelta(days=k)) is None]


@dataclass(frozen=True)
class GuardDecision:
    allowed: bool
    binding: str | None
    detail: str
    engine_state: str  # RUNNING | STOP


def check_entry(rung: str, policy: Mapping[str, Any], *, tier_capital_usd: float, deployed_usd: float,
                new_notional_usd: float, today: date, rota: OnCallRota) -> GuardDecision:
    mode = base_mode(rung)
    if mode in ("PAPER", "SHADOW"):
        return GuardDecision(False, "NO_LIVE_ORDERS", f"{mode} sends no orders", "RUNNING")
    who = rota.on_call(today)
    if who is None:
        return GuardDecision(False, "NO_ON_CALL", f"nobody on call on {today.isoformat()}: engine in STOP", "STOP")
    cap = capital_fraction(rung, policy) * tier_capital_usd
    if deployed_usd + new_notional_usd > cap + 1e-9:
        return GuardDecision(False, "RUNG_CAPITAL_CAP",
                             f"{rung} may deploy ${cap:,.0f}; ${deployed_usd:,.0f} deployed + ${new_notional_usd:,.0f} new",
                             "RUNNING")
    return GuardDecision(True, None, f"on call: {who}", "RUNNING")
