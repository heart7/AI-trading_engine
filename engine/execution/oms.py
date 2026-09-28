"""Order management (spec §8.2, §8.3, §8.7).

INV-30  An entry can never become a market order: entries are post-only, re-priced, then an IOC *limit*.
INV-31  Every fill and every ratchet is followed by a read-back of the venue-resident stop; a mismatch opens
        PROTECTION_UNVERIFIED and blocks entries on that venue.
INV-35  An order reaches a real venue only if that venue is TRADE_ENABLED for the product. PAPER mode only
        ever talks to the in-process simulated venue.
INV-39  No order at all is sent after a start or failover until restart reconciliation passes, and only the
        holder of the current fencing epoch may send.
INV-40  An ambiguous venue response marks the order UNKNOWN and sends the OMS to reconciliation. It is never
        treated as a rejection and never resubmitted.

Positions and stops are sized from filled quantity, never from requested quantity. Position truth comes from
fills (REST history on any stream gap), never from inference.
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field, replace

from engine.common.incidents import IncidentLog
from engine.execution.errors import ErrorClass, VenueError
from engine.execution.model import (
    ENTRY_PURPOSES,
    TERMINAL,
    Fill,
    OrderRequest,
    OrderStatus,
    OrderType,
    Purpose,
    VenueOrder,
)
from engine.execution.ratelimit import Lane, RateLimited, TokenBucket
from engine.execution.sim import InstrumentSpec
from engine.execution.venues import VENUE_BLOCKING, VenueRegistry
from engine.stops.stops import StopWidenAttempt

MAX_RETRIES = 3
LANE = {Purpose.PROTECT: Lane.PROTECT, Purpose.EXIT: Lane.PROTECT, Purpose.FLATTEN: Lane.PROTECT,
        Purpose.ENTRY: Lane.ENTRY, Purpose.ADD: Lane.ENTRY}


class OrderRefused(Exception):
    def __init__(self, code: str, detail: str = ""):
        self.code, self.detail = code, detail
        super().__init__(f"{code}: {detail}" if detail else code)


@dataclass
class Fence:
    """Leader lease. A standby that takes over bumps the epoch; the old leader's epoch is then stale."""

    epoch: int = 0

    def acquire(self) -> int:
        self.epoch += 1
        return self.epoch


@dataclass
class LocalOrder:
    req: OrderRequest
    status: OrderStatus = OrderStatus.PENDING
    filled: float = 0.0
    venue_order_id: str | None = None
    note: str = ""


@dataclass
class Journal:
    """What the engine database holds for one venue. Survives restarts; the venue is reconciled against it."""

    orders: dict[str, LocalOrder] = field(default_factory=dict)
    positions: dict[str, float] = field(default_factory=dict)  # instrument -> base qty held by the engine
    stops: dict[str, str] = field(default_factory=dict)  # instrument -> client id of the resting stop
    stop_targets: dict[str, float] = field(default_factory=dict)
    last_fill_seq: int = 0
    last_stream_seq: int = 0
    ledger: list[dict] = field(default_factory=list)
    next_id: int = 1


@dataclass
class ReconReport:
    ok: bool
    mismatches: list[str]
    resolved_unknown: list[str]


class OMS:
    def __init__(self, adapter, *, instruments: Mapping[str, InstrumentSpec], mode: str = "PAPER",
                 venues: VenueRegistry | None = None, product: str = "spot", fence: Fence | None = None,
                 journal: Journal | None = None, incidents: IncidentLog | None = None, bucket: TokenBucket | None = None,
                 clock_skew_max_ms: int = 500):
        if mode == "PAPER" and not adapter.paper:
            raise OrderRefused("PAPER_NEVER_SENDS", "PAPER mode runs only against the simulated venue")
        self.adapter, self.instruments, self.mode, self.venues, self.product = adapter, instruments, mode, venues, product
        self.fence = fence or Fence()
        self.epoch = self.fence.acquire()
        self.j = journal or Journal()
        self.incidents = incidents if incidents is not None else (venues.incidents if venues else IncidentLog())
        self.bucket = bucket or TokenBucket(capacity=20, refill_per_s=1.0)
        self.clock_skew_max_ms = clock_skew_max_ms
        self.scope = f"venue:{adapter.venue_id}"
        self.reconciled = False  # INV-39: nothing is sent until reconcile() passes
        self.now_s = 0.0

    # -- identity and gating ---------------------------------------------------------------------
    def new_client_id(self, tag: str) -> str:
        cid = f"u{self.epoch}-{self.j.next_id}-{tag}"
        self.j.next_id += 1
        return cid

    def entry_blocks(self) -> set[str]:
        codes = self.incidents.open_codes(self.scope) & (VENUE_BLOCKING | {"ORDER_STATE_UNKNOWN"})
        if self.venues is not None:
            codes |= self.venues.entries_blocked(self.adapter.venue_id)
        if not self.reconciled:
            codes.add("NOT_RECONCILED")
        return codes

    def _gate(self, req: OrderRequest) -> None:
        if self.fence.epoch != self.epoch:
            raise OrderRefused("NOT_LEADER", f"epoch {self.epoch} fenced by {self.fence.epoch}")
        if not self.reconciled:
            raise OrderRefused("NOT_RECONCILED", "restart reconciliation has not passed")
        if not self.adapter.paper:
            ok, code = self.venues.can_trade(self.adapter.venue_id, self.product) if self.venues else (False, "VENUE_NOT_TRADE_ENABLED")
            if not ok:
                raise OrderRefused(code, self.adapter.venue_id)
        if req.purpose in ENTRY_PURPOSES:
            if req.type not in (OrderType.POST_ONLY, OrderType.IOC) or req.price is None:
                raise OrderRefused("ENTRY_MUST_BE_LIMIT", "entries are post-only or IOC limit, never market")
            blocks = self.entry_blocks()
            if blocks:
                raise OrderRefused(sorted(blocks)[0], ", ".join(sorted(blocks)))
        if req.type == OrderType.STOP and not req.reduce_only:
            raise OrderRefused("STOP_MUST_BE_REDUCE_ONLY")

    # -- sending ---------------------------------------------------------------------------------
    def submit(self, req: OrderRequest) -> LocalOrder:
        existing = self.j.orders.get(req.client_id)
        if existing is not None:  # idempotent: the same client id is never sent twice
            return existing
        self._gate(req)
        lane = LANE.get(req.purpose, Lane.CANCEL)
        if not self.bucket.try_take(lane, self.now_s):
            raise RateLimited(lane)
        lo = LocalOrder(req)
        self.j.orders[req.client_id] = lo  # journal before sending: a crash mid-send leaves a PENDING row to reconcile
        for _ in range(MAX_RETRIES):
            try:
                vo = self.adapter.place(req)
            except VenueError as e:
                if e.cls == ErrorClass.RETRYABLE:
                    continue  # nothing happened on the venue; same client id
                if e.cls == ErrorClass.REJECTED:
                    lo.status, lo.note = OrderStatus.REJECTED, e.code
                    return lo
                self._unknown(lo, e)
                return lo
            self._adopt(lo, vo)
            self.sync_fills()
            return lo
        lo.status, lo.note = OrderStatus.REJECTED, "RETRIES_EXHAUSTED"
        return lo

    def _unknown(self, lo: LocalOrder, e: VenueError) -> None:
        lo.status, lo.note = OrderStatus.UNKNOWN, e.code
        self.reconciled = False
        self.incidents.open_incident("ORDER_STATE_UNKNOWN", "S2", self.scope, f"{lo.req.client_id}: {e.code}")

    def _adopt(self, lo: LocalOrder, vo: VenueOrder) -> None:
        lo.status, lo.venue_order_id = vo.status, vo.venue_order_id
        lo.req = replace(lo.req, qty=vo.qty, stop_price=vo.stop_price)

    def cancel(self, client_id: str) -> LocalOrder:
        lo = self.j.orders[client_id]
        if lo.status in TERMINAL:
            return lo
        try:
            vo = self.adapter.cancel(client_id)
        except VenueError as e:
            if e.cls == ErrorClass.REJECTED:
                vo = self.adapter.query(client_id)
                if vo is None:
                    lo.status = OrderStatus.CANCELLED
                    return lo
            else:
                self._unknown(lo, e)
                return lo
        self._adopt(lo, vo)
        self.sync_fills()
        return lo

    # -- fills and the stream --------------------------------------------------------------------
    def sync_fills(self) -> None:
        """REST fill history since the last applied fill. Idempotent by sequence number."""
        for f in self.adapter.fills_since(self.j.last_fill_seq):
            self._apply_fill(f)

    def _apply_fill(self, f: Fill) -> None:
        if f.seq <= self.j.last_fill_seq:
            return
        self.j.last_fill_seq = f.seq
        lo = self.j.orders.get(f.client_id)
        sign = 1 if f.side == "buy" else -1
        pos = self.j.positions.get(f.instrument_id, 0.0) + sign * f.qty
        self.j.positions[f.instrument_id] = round(pos, 12)
        self.j.ledger.append({"kind": "FILL", "client_id": f.client_id, "instrument_id": f.instrument_id, "side": f.side,
                              "qty": f.qty, "price": f.price, "fee": f.fee, "liquidity": f.liquidity, "seq": f.seq})
        if lo is not None:
            lo.filled = round(lo.filled + f.qty, 12)
            if lo.filled >= lo.req.qty - 1e-12:
                lo.status = OrderStatus.FILLED
            elif lo.status != OrderStatus.CANCELLED:  # an IOC residual stays cancelled
                lo.status = OrderStatus.PARTIAL
        stop_cid = self.j.stops.get(f.instrument_id)
        if stop_cid == f.client_id and lo is not None and lo.status == OrderStatus.FILLED:
            del self.j.stops[f.instrument_id]  # the protective stop did its job
            self.j.stop_targets.pop(f.instrument_id, None)
        self._write_off_dust(f.instrument_id, f.price)

    def _write_off_dust(self, instrument_id: str, price: float) -> None:
        spec = self.instruments[instrument_id]
        pos = self.j.positions.get(instrument_id, 0.0)
        tradable = math.floor(pos / spec.lot + 1e-9) * spec.lot
        if 0 < pos and (tradable * price < spec.min_notional) and instrument_id not in self.j.stops:
            self.j.ledger.append({"kind": "DUST_WRITE_OFF", "instrument_id": instrument_id, "qty": pos, "price": price})
            self.j.positions[instrument_id] = 0.0

    def process_stream(self, events: list[tuple[int, object]]) -> None:
        """Websocket delivery. A sequence gap triggers a REST resync; order state is never inferred across a gap."""
        for seq, ev in events:
            if seq <= self.j.last_stream_seq:
                continue
            if seq != self.j.last_stream_seq + 1:
                self.incidents.open_incident("WS_RESYNC_PENDING", "S3", self.scope,
                                             f"gap {self.j.last_stream_seq + 1}..{seq - 1}")
                self.resync()
                return
            self.j.last_stream_seq = seq
            if isinstance(ev, Fill):
                self._apply_fill(ev)
            elif isinstance(ev, tuple) and ev[0] == "order":
                vo: VenueOrder = ev[1]
                lo = self.j.orders.get(vo.client_id)
                if lo is not None and vo.status in (OrderStatus.CANCELLED, OrderStatus.OPEN):
                    lo.status = vo.status if lo.status not in TERMINAL else lo.status

    def resync(self) -> None:
        snap = self.adapter.snapshot()
        self.sync_fills()
        for vo in snap.open_orders:
            if vo.client_id in self.j.orders:
                self._adopt(self.j.orders[vo.client_id], vo)
        for cid, lo in self.j.orders.items():
            if lo.status in (OrderStatus.OPEN, OrderStatus.PARTIAL) and cid not in {o.client_id for o in snap.open_orders}:
                vo = self.adapter.query(cid)
                if vo is not None:
                    self._adopt(lo, vo)
        self.j.last_stream_seq = max(self.j.last_stream_seq, snap.seq)
        self.incidents.resolve("WS_RESYNC_PENDING", self.scope, "REST snapshot resync")

    # -- reconciliation --------------------------------------------------------------------------
    def reconcile(self, base_holdings: Mapping[str, float] | None = None) -> ReconReport:
        """Restart/failover reconciliation (spec §8.7). `base_holdings` is balance the engine does not manage."""
        base_holdings = base_holdings or {}
        mismatches: list[str] = []
        resolved: list[str] = []
        snap = self.adapter.snapshot()
        self.sync_fills()
        venue_open = {o.client_id: o for o in snap.open_orders}
        for cid, lo in self.j.orders.items():
            if lo.status in TERMINAL:
                continue
            vo = venue_open.get(cid) or self.adapter.query(cid)
            if vo is None:
                if lo.status in (OrderStatus.UNKNOWN, OrderStatus.PENDING):
                    lo.status, lo.note = OrderStatus.CANCELLED, "NOT_FOUND_AT_VENUE"  # never landed
                    resolved.append(cid)
                else:
                    mismatches.append(f"LOCAL_ORDER_MISSING {cid}")
                continue
            if lo.status == OrderStatus.UNKNOWN:
                resolved.append(cid)
            self._adopt(lo, vo)
        self.sync_fills()
        for cid in venue_open:
            if cid not in self.j.orders:
                mismatches.append(f"UNTRACKED_VENUE_ORDER {cid}")
        for inst, spec in self.instruments.items():
            venue_qty = snap.balances.get(spec.base, 0.0) - base_holdings.get(spec.base, 0.0)
            if abs(venue_qty - self.j.positions.get(inst, 0.0)) > spec.lot / 2:
                mismatches.append(f"POSITION_MISMATCH {inst} venue={venue_qty} engine={self.j.positions.get(inst, 0.0)}")
        self.j.last_stream_seq = max(self.j.last_stream_seq, snap.seq)
        if mismatches:
            self.reconciled = False
            self.incidents.open_incident("ORDER_STATE_MISMATCH", "S2", self.scope, "; ".join(mismatches))
        else:
            self.reconciled = True
            self.incidents.resolve("ORDER_STATE_MISMATCH", self.scope, "reconciled")
            self.incidents.resolve("ORDER_STATE_UNKNOWN", self.scope, "reconciled")
            for inst in list(self.j.stops):
                self.verify_protection(inst)
        return ReconReport(not mismatches, mismatches, resolved)

    def check_clock(self, local_ms: int) -> bool:
        skew = self.adapter.server_time_ms() - local_ms
        if abs(skew) > self.clock_skew_max_ms:
            if "CLOCK_SKEW" not in self.incidents.open_codes(self.scope):
                self.incidents.open_incident("CLOCK_SKEW", "S2", self.scope, f"{skew} ms")
            return False
        self.incidents.resolve("CLOCK_SKEW", self.scope, f"skew {skew} ms")
        return True

    # -- protection ------------------------------------------------------------------------------
    def _stop_qty(self, inst: str) -> float:
        spec = self.instruments[inst]
        return math.floor(self.j.positions.get(inst, 0.0) / spec.lot + 1e-9) * spec.lot

    def set_stop(self, inst: str, stop_price: float) -> bool:
        """Set or ratchet the protective stop for a long spot position. Stops never widen (INV-04)."""
        spec = self.instruments[inst]
        stop_price = math.floor(stop_price / spec.tick + 1e-9) * spec.tick
        cur = self.j.stop_targets.get(inst)
        if cur is not None and stop_price < cur - 1e-12:
            raise StopWidenAttempt(f"{inst} stop {cur} -> {stop_price}")
        self.j.stop_targets[inst] = stop_price
        return self.ensure_protection(inst)

    def ensure_protection(self, inst: str) -> bool:
        target = self.j.stop_targets.get(inst)
        qty = self._stop_qty(inst)
        cid = self.j.stops.get(inst)
        if qty <= 0 or target is None:
            if cid and self.j.orders[cid].status not in TERMINAL:
                self.cancel(cid)
            self.j.stops.pop(inst, None)
            return True
        if cid is not None:
            lo = self.j.orders[cid]
            try:
                if lo.status in TERMINAL:
                    raise VenueError(ErrorClass.REJECTED, "ORDER_NOT_OPEN")
                if self.adapter.supports_amend:
                    self._adopt(lo, self.adapter.amend(cid, qty=qty, stop_price=target))
                else:
                    self.cancel(cid)
                    self.sync_fills()
                    raise VenueError(ErrorClass.REJECTED, "REPLACED")
            except VenueError as e:
                if e.cls == ErrorClass.UNKNOWN_STATE:
                    self._unknown(lo, e)
                    return self.verify_protection(inst)
                # The old stop is gone. If it filled during the replace the position is closed: do not re-place.
                self.sync_fills()
                vo = self.adapter.query(cid)
                if vo is not None:
                    self._adopt(lo, vo)
                self.j.stops.pop(inst, None)
                if self._stop_qty(inst) <= 0:
                    self.j.stop_targets.pop(inst, None)
                    return True
                return self._place_stop(inst, self._stop_qty(inst), target)
            return self.verify_protection(inst)
        return self._place_stop(inst, qty, target)

    def _place_stop(self, inst: str, qty: float, target: float) -> bool:
        req = OrderRequest(self.new_client_id("stop"), inst, "sell", OrderType.STOP, qty, Purpose.PROTECT,
                           stop_price=target, reduce_only=True)
        lo = self.submit(req)
        if lo.status not in (OrderStatus.REJECTED, OrderStatus.CANCELLED):
            self.j.stops[inst] = req.client_id
        if lo.status == OrderStatus.FILLED:  # triggered at once (price already through the stop)
            self.j.stops.pop(inst, None)
            return True
        return self.verify_protection(inst)

    def verify_protection(self, inst: str, *, qty_tol_lots: float = 1.0, px_tol_ticks: float = 1.0) -> bool:
        """Read the venue order back and compare it with what the position needs (spec §8.3)."""
        spec = self.instruments[inst]
        need_qty, target = self._stop_qty(inst), self.j.stop_targets.get(inst)
        cid = self.j.stops.get(inst)
        problem = None
        if need_qty <= 0:
            problem = None
        elif cid is None:
            problem = "no stop resting"
        else:
            try:
                vo = self.adapter.query(cid)
            except VenueError as e:
                vo, problem = None, f"read-back failed: {e.code}"
            if problem is None:
                if vo is None or vo.status not in (OrderStatus.OPEN, OrderStatus.PARTIAL):
                    problem = f"stop {cid} not open ({vo.status.value if vo else 'missing'})"
                elif abs((vo.qty - vo.filled) - need_qty) > qty_tol_lots * spec.lot + 1e-12:
                    problem = f"stop qty {vo.qty - vo.filled} vs position {need_qty}"
                elif target is None or abs(vo.stop_price - target) > px_tol_ticks * spec.tick + 1e-12:
                    problem = f"stop price {vo.stop_price} vs target {target}"
        if problem:
            if "PROTECTION_UNVERIFIED" not in self.incidents.open_codes(self.scope):
                self.incidents.open_incident("PROTECTION_UNVERIFIED", "S1", self.scope, f"{inst}: {problem}")
            return False
        self.incidents.resolve("PROTECTION_UNVERIFIED", self.scope, f"{inst} verified")
        return True

    def on_entry_fills(self, inst: str) -> bool:
        """After any entry fill: size the stop to the filled quantity and verify it."""
        self.sync_fills()
        return self.ensure_protection(inst)
