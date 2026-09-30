"""Simulated venue for PAPER mode, the conformance suite and fault injection (spec §8.7, §9.7).

Nothing here leaves the process. Spot semantics: a sell (including a protective stop) needs the asset
balance; stops are reduce-only. Faults are scripted per call so every race in §8.7 is reproducible:

  sim.inject("place", "timeout_after_accept")   # order lands, the response is lost -> UNKNOWN_STATE
  sim.inject("place", "timeout_before_accept")  # nothing lands, the response is lost -> UNKNOWN_STATE
  sim.inject("place", "rate_limited")           # RETRYABLE, nothing happened
  sim.inject("amend", "fill_first")             # the stop fills while the engine replaces it
  sim.inject("cancel", "fill_first")
  sim.inject("amend", "silent_noop")            # venue acks but does not change the order (read-back must catch it)
  sim.inject("stream", "drop")                  # next event is lost -> sequence gap
"""
from __future__ import annotations

import itertools
import math
from collections import defaultdict, deque
from dataclasses import dataclass, field, replace

from engine.execution.errors import ErrorClass, Timeout, VenueError
from engine.execution.model import TERMINAL, Fill, OrderRequest, OrderStatus, OrderType, Snapshot, VenueOrder


@dataclass(frozen=True)
class InstrumentSpec:
    base: str
    quote: str
    lot: float
    tick: float
    min_notional: float


@dataclass
class Market:
    bid: float
    ask: float
    depth: float = math.inf  # base qty available at the touch for an aggressive order


@dataclass
class SimVenue:
    venue_id: str = "sim-kraken-spot"
    connector_type: str = "kraken-spot"
    connector_version: str = "0.1.0"
    environment: str = "sim"
    paper: bool = True
    supports_amend: bool = True
    instruments: dict[str, InstrumentSpec] = field(default_factory=dict)
    balances: dict[str, float] = field(default_factory=lambda: defaultdict(float))
    maker_fee: float = 0.0025
    taker_fee: float = 0.0040
    clock_ms: int = 1_790_000_000_000
    skew_ms: int = 0
    orders: dict[str, VenueOrder] = field(default_factory=dict)
    fills: list[Fill] = field(default_factory=list)
    stream: list[tuple[int, object]] = field(default_factory=list)  # (seq, event) as a websocket would deliver
    markets: dict[str, Market] = field(default_factory=dict)
    faults: dict[str, deque] = field(default_factory=lambda: defaultdict(deque))
    dead_man_deadline_ms: int | None = None
    calls: list[str] = field(default_factory=list)
    _seq: itertools.count = field(default_factory=lambda: itertools.count(1))
    _oid: itertools.count = field(default_factory=lambda: itertools.count(1))

    # -- test controls ---------------------------------------------------------------------------
    def inject(self, method: str, fault: str) -> None:
        self.faults[method].append(fault)

    def _fault(self, method: str) -> str | None:
        q = self.faults.get(method)
        return q.popleft() if q else None

    def set_market(self, instrument_id: str, bid: float, ask: float, depth: float = math.inf) -> None:
        self.markets[instrument_id] = Market(bid, ask, depth)
        self._match(instrument_id)

    def advance(self, seconds: float) -> None:
        self.clock_ms += int(seconds * 1000)
        if self.dead_man_deadline_ms is not None and self.clock_ms >= self.dead_man_deadline_ms:
            for o in self.orders.values():
                if o.status in (OrderStatus.OPEN, OrderStatus.PARTIAL) and o.type != OrderType.STOP:
                    o.status = OrderStatus.CANCELLED
                    self._emit(("order", replace(o)))
            self.dead_man_deadline_ms = None

    # -- internals -------------------------------------------------------------------------------
    def _emit(self, event: object) -> None:
        seq = next(self._seq)
        if self._fault("stream") == "drop":
            return  # the event happened but the stream lost it
        self.stream.append((seq, event))

    def _spec(self, instrument_id: str) -> InstrumentSpec:
        if instrument_id not in self.instruments:
            raise VenueError(ErrorClass.REJECTED, "UNKNOWN_INSTRUMENT", instrument_id)
        return self.instruments[instrument_id]

    def _fill(self, o: VenueOrder, qty: float, price: float, liquidity: str) -> None:
        spec = self.instruments[o.instrument_id]
        qty = min(qty, o.qty - o.filled)
        if qty <= 0:
            return
        notional = qty * price
        fee = notional * (self.maker_fee if liquidity == "maker" else self.taker_fee)
        if o.side == "buy":
            self.balances[spec.quote] -= notional + fee
            self.balances[spec.base] += qty
        else:
            self.balances[spec.base] -= qty
            self.balances[spec.quote] += notional - fee
        o.filled = round(o.filled + qty, 12)
        o.status = OrderStatus.FILLED if o.filled >= o.qty - 1e-12 else OrderStatus.PARTIAL
        seq = next(self._seq)
        f = Fill(o.client_id, o.instrument_id, o.side, qty, price, fee, liquidity, seq)
        self.fills.append(f)
        if self._fault("stream") != "drop":
            self.stream.append((seq, f))
        self._emit(("order", replace(o)))

    def _match(self, instrument_id: str) -> None:
        m = self.markets.get(instrument_id)
        if not m:
            return
        for o in list(self.orders.values()):
            if o.instrument_id != instrument_id or o.status not in (OrderStatus.OPEN, OrderStatus.PARTIAL):
                continue
            if o.type == OrderType.POST_ONLY:
                if (o.side == "buy" and m.ask <= o.price) or (o.side == "sell" and m.bid >= o.price):
                    self._fill(o, o.qty - o.filled, o.price, "maker")
            elif o.type == OrderType.STOP:
                if (o.side == "sell" and m.bid <= o.stop_price) or (o.side == "buy" and m.ask >= o.stop_price):
                    px = m.bid if o.side == "sell" else m.ask
                    self._fill(o, o.qty - o.filled, px, "taker")

    def _free(self, asset: str, exclude: str | None = None) -> float:
        """Balance not reserved by open sell orders (spot venues reserve the asset for a resting sell)."""
        reserved = sum(o.qty - o.filled for o in self.orders.values()
                       if o.side == "sell" and o.status in (OrderStatus.OPEN, OrderStatus.PARTIAL)
                       and self.instruments[o.instrument_id].base == asset and o.client_id != exclude)
        return self.balances[asset] - reserved

    # -- adapter interface -----------------------------------------------------------------------
    def server_time_ms(self) -> int:
        return self.clock_ms + self.skew_ms

    def place(self, req: OrderRequest) -> VenueOrder:
        self.calls.append(f"place:{req.client_id}:{req.type.value}")
        fault = self._fault("place")
        if fault == "rate_limited":
            raise VenueError(ErrorClass.RETRYABLE, "RATE_LIMIT")
        if fault == "reject":
            raise VenueError(ErrorClass.REJECTED, "INSUFFICIENT_FUNDS")
        if fault in ("timeout_before_accept", "ambiguous"):
            raise Timeout() if fault == "timeout_before_accept" else VenueError(ErrorClass.UNKNOWN_STATE, "SERVICE_BUSY")
        if req.client_id in self.orders:  # duplicate-submit protection: same client id -> same order
            return replace(self.orders[req.client_id])
        spec = self._spec(req.instrument_id)
        m = self.markets.get(req.instrument_id)
        if req.qty <= 0 or abs(req.qty / spec.lot - round(req.qty / spec.lot)) > 1e-6:
            raise VenueError(ErrorClass.REJECTED, "INVALID_LOT")
        px_ref = req.price or req.stop_price or (m.ask if m else 0)
        if req.qty * px_ref < spec.min_notional:
            raise VenueError(ErrorClass.REJECTED, "MIN_NOTIONAL")
        if req.side == "sell" and self._free(spec.base) < req.qty - 1e-12:
            raise VenueError(ErrorClass.REJECTED, "INSUFFICIENT_FUNDS", "sell exceeds free balance")
        if req.type == OrderType.POST_ONLY and m and ((req.side == "buy" and req.price >= m.ask)
                                                      or (req.side == "sell" and req.price <= m.bid)):
            raise VenueError(ErrorClass.REJECTED, "POST_ONLY_WOULD_TAKE")
        o = VenueOrder(req.client_id, f"V{next(self._oid)}", req.instrument_id, req.side, req.type, req.qty, 0.0,
                       OrderStatus.OPEN, req.price, req.stop_price, req.reduce_only)
        self.orders[req.client_id] = o
        self._emit(("order", replace(o)))
        if req.type in (OrderType.IOC, OrderType.MARKET) and m:
            touch = m.ask if req.side == "buy" else m.bid
            marketable = req.type == OrderType.MARKET or (req.side == "buy" and req.price >= touch) \
                or (req.side == "sell" and req.price <= touch)
            if marketable:
                self._fill(o, min(o.qty, m.depth), touch, "taker")
            if o.status != OrderStatus.FILLED:
                o.status = OrderStatus.CANCELLED  # IOC residual is cancelled
                self._emit(("order", replace(o)))
        elif req.type == OrderType.STOP:
            self._match(req.instrument_id)
        if fault == "timeout_after_accept":
            raise Timeout("response lost after the venue accepted the order")
        return replace(o)

    def amend(self, client_id: str, *, qty: float | None = None, stop_price: float | None = None) -> VenueOrder:
        self.calls.append(f"amend:{client_id}")
        o = self.orders.get(client_id)
        if o is None:
            raise VenueError(ErrorClass.REJECTED, "UNKNOWN_ORDER")
        fault = self._fault("amend")
        if fault == "fill_first":
            m = self.markets[o.instrument_id]
            self._fill(o, o.qty - o.filled, m.bid if o.side == "sell" else m.ask, "taker")
        if fault == "timeout":
            raise Timeout()
        if o.status in TERMINAL:
            raise VenueError(ErrorClass.REJECTED, "ORDER_NOT_OPEN", o.status.value)
        if fault != "silent_noop":
            if qty is not None:
                spec = self.instruments[o.instrument_id]
                if o.side == "sell" and self._free(spec.base, exclude=client_id) < qty - o.filled - 1e-12:
                    raise VenueError(ErrorClass.REJECTED, "INSUFFICIENT_FUNDS")
                o.qty = qty
            if stop_price is not None:
                o.stop_price = stop_price
            self._emit(("order", replace(o)))
            self._match(o.instrument_id)
        return replace(o)

    def cancel(self, client_id: str) -> VenueOrder:
        self.calls.append(f"cancel:{client_id}")
        o = self.orders.get(client_id)
        if o is None:
            raise VenueError(ErrorClass.REJECTED, "UNKNOWN_ORDER")
        fault = self._fault("cancel")
        if fault == "fill_first":
            m = self.markets[o.instrument_id]
            self._fill(o, o.qty - o.filled, m.bid if o.side == "sell" else m.ask, "taker")
        if fault == "timeout":
            raise Timeout()
        if o.status in (OrderStatus.OPEN, OrderStatus.PARTIAL):
            o.status = OrderStatus.CANCELLED
            self._emit(("order", replace(o)))
        return replace(o)

    def query(self, client_id: str) -> VenueOrder | None:
        if self._fault("query") == "timeout":
            raise Timeout()
        o = self.orders.get(client_id)
        return replace(o) if o else None

    def snapshot(self) -> Snapshot:
        return Snapshot([replace(o) for o in self.orders.values() if o.status in (OrderStatus.OPEN, OrderStatus.PARTIAL)],
                        {k: v for k, v in self.balances.items() if abs(v) > 1e-12},
                        max((s for s, _ in self.stream), default=0))

    def fills_since(self, seq: int) -> list[Fill]:
        return [f for f in self.fills if f.seq > seq]

    def cancel_all_after(self, seconds: int) -> None:
        self.dead_man_deadline_ms = self.clock_ms + seconds * 1000 if seconds > 0 else None

    def events_since(self, seq: int) -> list[tuple[int, object]]:
        return [(s, e) for s, e in self.stream if s > seq]
