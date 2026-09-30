"""Unitised performance (spec §12.5, INV-41).

The fund is measured per unit. A deposit buys units at the current unit value and a withdrawal redeems them, so
neither changes the unit value: time-weighted return, high-water mark and drawdown are all on unit value, and a
deposit can neither reset a drawdown nor create P&L. Money-weighted return (IRR) is reported beside TWR.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from engine.evidence.ledger import dec

UNIT0 = Decimal(100)


@dataclass
class UnitFund:
    units: Decimal = Decimal(0)
    unit_value: Decimal = UNIT0
    hwm: Decimal = UNIT0
    history: list[tuple[datetime, str, Decimal, Decimal]] = field(default_factory=list)  # (ts, kind, nav, unit value)
    flows: list[tuple[datetime, Decimal]] = field(default_factory=list)  # external cash flows for IRR (+in, -out)

    @property
    def nav(self) -> Decimal:
        return self.units * self.unit_value

    def mark(self, ts: datetime, nav: float | Decimal) -> None:
        """Revalue at a NAV that contains no flow since the last mark."""
        nav = dec(nav)
        if self.units > 0:
            self.unit_value = nav / self.units
        self.hwm = max(self.hwm, self.unit_value)
        self.history.append((ts, "MARK", nav, self.unit_value))

    def deposit(self, ts: datetime, amount: float | Decimal, nav_before: float | Decimal) -> Decimal:
        """Mark at the pre-flow NAV, then issue units at that unit value. Returns units issued."""
        if self.units > 0:
            self.mark(ts, nav_before)
        a = dec(amount)
        issued = a / self.unit_value
        self.units += issued
        self.flows.append((ts, a))
        self.history.append((ts, "DEPOSIT", self.nav, self.unit_value))
        return issued

    def withdraw(self, ts: datetime, amount: float | Decimal, nav_before: float | Decimal) -> Decimal:
        self.mark(ts, nav_before)
        a = dec(amount)
        redeemed = a / self.unit_value
        if redeemed > self.units:
            raise ValueError("withdrawal exceeds fund")
        self.units -= redeemed
        self.flows.append((ts, -a))
        self.history.append((ts, "WITHDRAWAL", self.nav, self.unit_value))
        return redeemed

    @property
    def drawdown(self) -> Decimal:
        return 1 - self.unit_value / self.hwm

    @property
    def twr(self) -> Decimal:
        return self.unit_value / UNIT0 - 1

    def irr(self, ts_end: datetime) -> float:
        """Money-weighted annual return by bisection on NPV = 0 (flows in, terminal NAV out)."""
        if not self.flows:
            return 0.0
        t0 = self.flows[0][0]
        cfs = [((t - t0).total_seconds() / 31_557_600, -float(a)) for t, a in self.flows]
        cfs.append(((ts_end - t0).total_seconds() / 31_557_600, float(self.nav)))

        def npv(r: float) -> float:
            return sum(cf / (1 + r) ** t for t, cf in cfs)
        lo, hi = -0.99, 10.0
        if npv(lo) * npv(hi) > 0:
            return float("nan")
        for _ in range(200):
            mid = (lo + hi) / 2
            if npv(lo) * npv(mid) <= 0:
                hi = mid
            else:
                lo = mid
        return (lo + hi) / 2
