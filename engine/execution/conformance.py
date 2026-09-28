"""Adapter conformance suite (spec §8.7). A connector version must pass it on the venue's testnet or demo before
any instance of it can be PAPER_ENABLED, and again on every connector upgrade.

Checks tagged `sim` need scripted faults (lost responses, fills racing a replace, dropped stream events) and run
against the simulated venue; on a testnet they are reported NOT_RUN and the verdict needs the venue checks only
plus a sim PASS for the same connector version. Output is a run record keyed by a content-hashed run_id.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from engine.common.canonical import content_hash
from engine.common.incidents import IncidentLog
from engine.execution.errors import ErrorClass, classify
from engine.execution.ladder import DeadMan, EntryWorker
from engine.execution.model import OrderRequest, OrderStatus, OrderType, Purpose
from engine.execution.oms import OMS, OrderRefused
from engine.execution.ratelimit import Lane, TokenBucket
from engine.execution.sim import InstrumentSpec, SimVenue
from engine.stops.stops import round_order

INST = "BTC-USD"
SPEC = InstrumentSpec("BTC", "USD", lot=0.0001, tick=0.1, min_notional=5.0)


@dataclass(frozen=True)
class CheckResult:
    id: str
    passed: bool | None  # None = NOT_RUN
    detail: str
    needs: str = "venue"  # venue | sim


def _sim() -> SimVenue:
    v = SimVenue(instruments={INST: SPEC})
    v.balances["USD"] = 100_000.0
    v.set_market(INST, 60_000.0, 60_010.0)
    return v


def _oms(v: SimVenue, **kw: Any) -> OMS:
    o = OMS(v, instruments={INST: SPEC}, **kw)
    o.reconcile()
    return o


def _buy_ioc(o: OMS, qty: float, px: float) -> Any:
    return o.submit(OrderRequest(o.new_client_id("t"), INST, "buy", OrderType.IOC, qty, Purpose.ENTRY, price=px))


def check_lifecycle() -> CheckResult:
    v = _sim()
    o = _oms(v)
    po = o.submit(OrderRequest(o.new_client_id("po"), INST, "buy", OrderType.POST_ONLY, 0.01, Purpose.ENTRY, price=59_990.0))
    ok1 = po.status == OrderStatus.OPEN
    o.cancel(po.req.client_id)
    ok2 = o.j.orders[po.req.client_id].status == OrderStatus.CANCELLED
    ioc = _buy_ioc(o, 0.01, 60_010.0)
    ok3 = ioc.status == OrderStatus.FILLED and not v.snapshot().open_orders
    ok4 = o.set_stop(INST, 57_000.0) and o.set_stop(INST, 57_500.0)
    stop = v.query(o.j.stops[INST])
    ok5 = stop is not None and stop.stop_price == 57_500.0 and abs(stop.qty - 0.01) < 1e-12
    return CheckResult("LIFECYCLE", all((ok1, ok2, ok3, ok4, ok5)),
                       f"post-only open={ok1} cancel={ok2} ioc={ok3} stop place/amend={ok4} read-back={ok5}")


def check_idempotent() -> CheckResult:
    v = _sim()
    o = _oms(v)
    req = OrderRequest("dup-1", INST, "buy", OrderType.POST_ONLY, 0.01, Purpose.ENTRY, price=59_990.0)
    o.submit(req)
    o.submit(req)
    direct = v.place(req)  # a raw duplicate to the venue must map to the same order
    n = sum(1 for c in v.calls if c.startswith("place:dup-1"))
    return CheckResult("IDEMPOTENT_CLIENT_ID", len(v.orders) == 1 and direct.client_id == "dup-1" and n == 2,
                       f"venue orders={len(v.orders)}, OMS sends={n - 1}")


def check_partial_fill() -> CheckResult:
    v = _sim()
    o = _oms(v)
    v.set_market(INST, 60_000.0, 60_010.0, depth=0.0037)
    lo = _buy_ioc(o, 0.01, 60_010.0)
    o.set_stop(INST, 57_000.0)
    stop = v.query(o.j.stops[INST])
    ok = lo.status == OrderStatus.CANCELLED and abs(lo.filled - 0.0037) < 1e-12 and stop and abs(stop.qty - 0.0037) < 1e-12
    return CheckResult("PARTIAL_FILL_SIZING", bool(ok), f"filled={lo.filled}, stop qty={stop.qty if stop else None}")


def check_rounding() -> CheckResult:
    q, s, risk = round_order(0.012345, 57_123.456, 60_000.0, lot=SPEC.lot, tick=SPEC.tick, side=1, max_risk_usd=35.0)
    lot_ok = abs(q / SPEC.lot - round(q / SPEC.lot)) < 1e-6 and q <= 0.012345
    tick_ok = abs(s / SPEC.tick - round(s / SPEC.tick)) < 1e-6 and 57_123.456 - SPEC.tick <= s <= 57_123.456
    return CheckResult("ROUNDING", lot_ok and tick_ok and risk <= 35.0, f"qty={q} stop={s} risk={risk:.2f}")


def check_cancel_fill_race() -> CheckResult:
    results = []
    for method in ("amend", "cancel"):
        v = _sim()
        v.supports_amend = method == "amend"
        o = _oms(v)
        _buy_ioc(o, 0.01, 60_010.0)
        o.set_stop(INST, 57_000.0)
        v.set_market(INST, 57_900.0, 57_910.0)
        v.inject(method, "fill_first")
        o.set_stop(INST, 58_000.0)
        sells = [f for f in v.fills if f.side == "sell"]
        results.append((method, abs(v.balances["BTC"]) < 1e-12 and len(sells) == 1 and INST not in o.j.stops
                        and o.j.positions[INST] == 0.0))
    return CheckResult("CANCEL_FILL_RACE", all(r for _, r in results), f"{results}", needs="sim")


def check_restart_reconciliation() -> CheckResult:
    v = _sim()
    o = _oms(v)
    _buy_ioc(o, 0.01, 60_010.0)
    o.set_stop(INST, 57_000.0)
    o2 = OMS(v, instruments={INST: SPEC}, journal=o.j, fence=o.fence)
    try:
        _buy_ioc(o2, 0.01, 60_010.0)
        refused = False
    except OrderRefused as e:
        refused = e.code == "NOT_RECONCILED"
    rep = o2.reconcile()
    try:
        _buy_ioc(o, 0.001, 60_010.0)
        fenced = False
    except OrderRefused as e:
        fenced = e.code == "NOT_LEADER"
    return CheckResult("RESTART_RECONCILIATION", refused and rep.ok and fenced,
                       f"refused before recon={refused}, recon ok={rep.ok}, old leader fenced={fenced}")


def check_unknown_state() -> CheckResult:
    v = _sim()
    o = _oms(v)
    v.inject("place", "timeout_after_accept")
    lo = _buy_ioc(o, 0.01, 60_010.0)
    sends = sum(1 for c in v.calls if c.startswith(f"place:{lo.req.client_id}"))
    was_unknown = lo.status == OrderStatus.UNKNOWN and not o.reconciled
    rep = o.reconcile()
    return CheckResult("UNKNOWN_NEVER_REJECTED", was_unknown and sends == 1 and rep.ok and lo.status == OrderStatus.FILLED
                       and abs(o.j.positions[INST] - 0.01) < 1e-12,
                       f"unknown={was_unknown}, sends={sends}, after recon={lo.status.value}", needs="sim")


def check_ws_gap() -> CheckResult:
    v = _sim()
    o = _oms(v)
    o.process_stream(v.events_since(0))
    v.inject("stream", "drop")
    _buy_ioc(o, 0.01, 60_010.0)
    inc = IncidentLog()
    o.incidents = inc
    o.process_stream(v.events_since(o.j.last_stream_seq))
    gap_seen = any(i.code == "WS_RESYNC_PENDING" for i in inc.items)
    return CheckResult("WS_GAP_RESYNC", gap_seen and "WS_RESYNC_PENDING" not in inc.open_codes()
                       and abs(o.j.positions[INST] - 0.01) < 1e-12, f"gap detected={gap_seen}", needs="sim")


def check_rate_lanes() -> CheckResult:
    b = TokenBucket(capacity=8, refill_per_s=0.0)
    entries = sum(b.try_take(Lane.ENTRY, 0) for _ in range(20))
    data_after = b.try_take(Lane.DATA, 0)
    protects = sum(b.try_take(Lane.PROTECT, 0) for _ in range(20))
    return CheckResult("RATE_LANES", entries == 6 and not data_after and protects == 2,
                       f"entries took {entries}/8, protective exits still got {protects}")


def check_clock() -> CheckResult:
    v = _sim()
    o = _oms(v)
    v.skew_ms = 900
    skew_ok = o.check_clock(v.clock_ms)
    try:
        _buy_ioc(o, 0.01, 60_010.0)
        blocked = False
    except OrderRefused as e:
        blocked = e.code == "CLOCK_SKEW"
    return CheckResult("CLOCK_SKEW", not skew_ok and blocked, f"blocked={blocked}", needs="sim")


def check_dead_man() -> CheckResult:
    v = _sim()
    o = _oms(v)
    _buy_ioc(o, 0.01, 60_010.0)
    o.set_stop(INST, 57_000.0)
    w = EntryWorker(o, INST, 0.01, bar_close_s=0, limit_max=61_000.0)
    w.step(1, 60_000.0, 60_010.0)
    dm = DeadMan(o, spares_stops=True)
    dm.refresh(1)
    v.advance(120)  # engine silent past the timer
    entry_cancelled = v.query(w.working).status == OrderStatus.CANCELLED if w.working else False
    stop_alive = v.query(o.j.stops[INST]).status == OrderStatus.OPEN
    return CheckResult("DEAD_MAN", entry_cancelled and stop_alive, f"entry cancelled={entry_cancelled}, stop alive={stop_alive}",
                       needs="sim")


def check_error_map(errors: dict[str, ErrorClass]) -> CheckResult:
    ok = classify(errors, "EZZZ:Something new") == ErrorClass.UNKNOWN_STATE
    return CheckResult("ERROR_MAP_DEFAULT_UNKNOWN", ok, "unmapped venue error classifies as UNKNOWN_STATE")


VENUE_CHECKS: tuple[Callable[[], CheckResult], ...] = (check_lifecycle, check_idempotent, check_partial_fill,
                                                        check_rounding, check_restart_reconciliation, check_rate_lanes)
SIM_CHECKS: tuple[Callable[[], CheckResult], ...] = (check_cancel_fill_race, check_unknown_state, check_ws_gap,
                                                      check_clock, check_dead_man)


def run_suite(connector_type: str, connector_version: str, errors: dict[str, ErrorClass], *, environment: str = "sim",
              now: datetime | None = None) -> dict[str, Any]:
    """Runs every check against the simulated venue. A testnet run plugs the same checks into a testnet adapter."""
    checks = [c() for c in VENUE_CHECKS + SIM_CHECKS] + [check_error_map(errors)]
    body = {"connector_type": connector_type, "connector_version": connector_version, "environment": environment,
            "checks": [c.__dict__ for c in checks],
            "verdict": "PASS" if all(c.passed for c in checks) else "FAIL",
            "at": (now or datetime.now(timezone.utc)).isoformat()}
    body["run_id"] = "conf-" + content_hash(body)[:16]
    return body
