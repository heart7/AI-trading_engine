"""Entry ladder (spec §8.2, INV-30): post-only at the touch, re-priced, then an IOC *limit* at the deadline.

The deadline is 15 minutes after the 4h bar close. Re-price interval and the IOC cap are ASSUMED
(A-ENTRY-LADDER). There is no code path that produces a market order for an entry.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from engine.execution.model import TERMINAL, OrderRequest, OrderStatus, OrderType, Purpose
from engine.execution.oms import OMS, OrderRefused


@dataclass(frozen=True)
class LadderParams:
    deadline_s: float = 15 * 60
    reprice_s: float = 60
    ioc_cap_bp: float = 20


@dataclass
class EntryWorker:
    oms: OMS
    instrument_id: str
    qty: float
    bar_close_s: float
    limit_max: float  # highest price the sizing allows (risk to the stop stays within r_tier x NAV)
    purpose: Purpose = Purpose.ENTRY
    params: LadderParams = field(default_factory=LadderParams)
    working: str | None = None
    placed_at: float = -math.inf
    done: bool = False
    history: list[str] = field(default_factory=list)

    def filled(self) -> float:
        return sum(lo.filled for cid, lo in self.oms.j.orders.items() if cid in self.history)

    def remaining(self) -> float:
        spec = self.oms.instruments[self.instrument_id]
        return math.floor((self.qty - self.filled()) / spec.lot + 1e-9) * spec.lot

    def _send(self, type_: OrderType, price: float) -> None:
        spec = self.oms.instruments[self.instrument_id]
        price = math.floor(price / spec.tick + 1e-9) * spec.tick  # a buy limit rounds down: never pays more
        req = OrderRequest(self.oms.new_client_id("ent"), self.instrument_id, "buy", type_, self.remaining(),
                           self.purpose, price=price)
        self.history.append(req.client_id)
        lo = self.oms.submit(req)
        self.working = req.client_id if lo.status in (OrderStatus.OPEN, OrderStatus.PARTIAL) else None

    def step(self, now_s: float, bid: float, ask: float) -> None:
        if self.done:
            return
        self.oms.now_s = now_s
        self.oms.sync_fills()
        if self.working and self.oms.j.orders[self.working].status in TERMINAL:
            self.working = None
        if self.remaining() <= 0:
            self.done = True
            return
        try:
            if now_s >= self.bar_close_s + self.params.deadline_s:
                if self.working:
                    self.oms.cancel(self.working)
                    self.working = None
                    if self.remaining() <= 0:
                        self.done = True
                        return
                cap = min(ask * (1 + self.params.ioc_cap_bp / 1e4), self.limit_max)
                if cap >= ask:
                    self._send(OrderType.IOC, cap)
                self.done = True  # any IOC residual is abandoned, never chased with a market order
                return
            touch = min(bid, self.limit_max)
            if self.working:
                lo = self.oms.j.orders[self.working]
                if lo.req.price >= touch or now_s - self.placed_at < self.params.reprice_s:
                    return
                self.oms.cancel(self.working)
                self.working = None
                if self.remaining() <= 0:
                    self.done = True
                    return
            if touch < ask:
                self._send(OrderType.POST_ONLY, touch)
                self.placed_at = now_s
        except OrderRefused as e:
            self.history.append(f"refused:{e.code}")
            self.done = True


@dataclass
class DeadMan:
    """Arms the venue cancel-all-after timer while entry orders work (spec §8.7). Only used where the venue's timer
    spares resting stops; protective stops must survive an engine outage."""

    oms: OMS
    spares_stops: bool
    refresh_s: float = 30
    timeout_s: int = 60
    last: float = -math.inf

    def refresh(self, now_s: float) -> bool:
        if not self.spares_stops:
            return False
        working = [lo for lo in self.oms.j.orders.values()
                   if lo.req.purpose in (Purpose.ENTRY, Purpose.ADD) and lo.status in (OrderStatus.OPEN, OrderStatus.PARTIAL)]
        if not working:
            if self.last > -math.inf:
                self.oms.adapter.cancel_all_after(0)
                self.last = -math.inf
            return False
        if now_s - self.last >= self.refresh_s:
            self.oms.adapter.cancel_all_after(self.timeout_s)
            self.last = now_s
        return True
