import random

import pytest

from engine.execution.adapters import probes
from engine.execution.adapters.kraken_spot import ERRORS, KrakenSpotAdapter, sign
from engine.execution.adapters.readonly import BinanceSpotReadOnly, binance_sign
from engine.execution.conformance import INST, SPEC, _sim, run_suite
from engine.execution.errors import ErrorClass, VenueError, classify
from engine.execution.ladder import DeadMan, EntryWorker
from engine.execution.model import OrderRequest, OrderStatus, OrderType, Purpose
from engine.execution.oms import OMS, OrderRefused
from engine.execution.ratelimit import Lane, TokenBucket
from engine.execution.registry import ConnectorRegistry
from engine.execution.venues import VenueRefused
from engine.secrets.store import SecretValue
from engine.stops.stops import StopWidenAttempt
from tests.helpers.venues import FAKE, NOW, READ, Principal, paper_enabled, registry


def test_kraken_signature_matches_published_example():
    secret = "kQH5HW/8p1uGOVjbgWA7FunAmGO8lsSUXNsu3eow76sz84Q18fWxnyRzBHCd3pd5nE9qa99HAZtuZuj6F1huXg=="
    body = "nonce=1616492376594&ordertype=limit&pair=XBTUSD&price=37500&type=buy&volume=1.25"
    assert sign("/0/private/AddOrder", "1616492376594", body, secret) == \
        "4/dpxb3iT4tp/ZCVEwSnEsLxx0bqyhLpdfOpc6fn7OR8+UClSV5n9E6aSS8MPtnRfp32bAb0nmbRn6H8ndwLUQ=="


def test_binance_signature_matches_published_example():
    q = "symbol=LTCBTC&side=BUY&type=LIMIT&timeInForce=GTC&quantity=1&price=0.1&recvWindow=5000&timestamp=1499827319559"
    assert binance_sign(q, "NhqPtmdSJYdKjVHjA7PZj4Mge3R5YNiP1e3UZjInClVN65XAbvqqM6A7H5fATj0j") == \
        "c8db56825ae71d6d79447849e617115f4a920fa2acdcab2b053c4b2838bd6b71"


def kraken(responses):
    sent = []

    def transport(method, url, headers, body):
        sent.append((method, url, dict(headers), body))
        r = responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r
    a = KrakenSpotAdapter("kr", "demo", api_key=SecretValue("k"), api_secret=SecretValue("c2VjcmV0"),
                          transport=transport, nonce=iter(range(1, 99)).__next__, pairs={"BTC-USD": "XBTUSD"})
    return a, sent


def test_kraken_order_params_entry_and_stop():
    a, _ = kraken([])
    p = a.order_params(OrderRequest("c1", "BTC-USD", "buy", OrderType.POST_ONLY, 0.01, Purpose.ENTRY, price=60000.0))
    assert p["oflags"] == "post" and p["ordertype"] == "limit" and p["cl_ord_id"] == "c1"
    p = a.order_params(OrderRequest("c2", "BTC-USD", "buy", OrderType.IOC, 0.01, Purpose.ENTRY, price=60010.0))
    assert p["timeinforce"] == "IOC" and p["ordertype"] == "limit"
    p = a.order_params(OrderRequest("c3", "BTC-USD", "sell", OrderType.STOP, 0.01, Purpose.PROTECT, stop_price=57000.0,
                                    reduce_only=True))
    assert p["ordertype"] == "stop-loss"


@pytest.mark.parametrize("resp,cls", [((200, {"error": ["EOrder:Insufficient funds"]}), ErrorClass.REJECTED),
                                      ((200, {"error": ["EAPI:Rate limit exceeded"]}), ErrorClass.RETRYABLE),
                                      ((200, {"error": ["EService:Unavailable"]}), ErrorClass.UNKNOWN_STATE),
                                      ((200, {"error": ["ENew:Never seen"]}), ErrorClass.UNKNOWN_STATE),
                                      ((502, {}), ErrorClass.UNKNOWN_STATE),
                                      (TimeoutError("read timed out"), ErrorClass.UNKNOWN_STATE)])
def test_kraken_error_mapping(resp, cls):
    a, sent = kraken([resp])
    with pytest.raises(VenueError) as e:
        a.place(OrderRequest("c1", "BTC-USD", "buy", OrderType.IOC, 0.01, Purpose.ENTRY, price=1.0))
    assert e.value.cls == cls
    assert sent[0][2]["API-Sign"] and "k" == sent[0][2]["API-Key"]


def test_classify_prefix_and_default():
    assert classify(ERRORS, "EOrder:Invalid price:XBTUSD") == ErrorClass.REJECTED
    assert classify({}, "anything") == ErrorClass.UNKNOWN_STATE


def test_readonly_connectors_cannot_place():
    a = BinanceSpotReadOnly(api_key=SecretValue("k"), api_secret=SecretValue("s"), transport=None, clock_ms=lambda: 0)
    with pytest.raises(VenueError) as e:
        a.place()
    assert e.value.code == "READ_ONLY_CONNECTOR"


def test_probe_parsers_fail_closed():
    ok = probes.binance_api_restrictions({"enableReading": True, "enableSpotAndMarginTrading": False,
                                          "enableWithdrawals": False, "enableInternalTransfer": False,
                                          "enableFutures": False, "ipRestrict": True})
    assert ok.read and not ok.trade_capable and not ok.withdraw and ok.ip_restricted
    assert probes.binance_api_restrictions({}).withdraw  # missing fields -> assume the worst
    assert probes.bybit_query_api({"retCode": 10003}).withdraw
    by = probes.bybit_query_api({"retCode": 0, "result": {"readOnly": 1, "permissions": {"Spot": ["SpotTrade"],
                                                                                         "Wallet": []}, "ips": ["1.2.3.4"]}})
    assert not by.trade and not by.withdraw and by.ip_restricted
    kr = probes.kraken_attested(attested={"query_funds": True}, withdraw_negative_probe_error=None)
    assert kr.withdraw and kr.trust == "ATTESTED"  # negative probe did not prove withdrawal is denied


def test_conformance_suite_passes_on_sim_and_run_id_is_stable():
    a = run_suite("kraken-spot", "0.1.0", ERRORS, now=NOW)
    b = run_suite("kraken-spot", "0.1.0", ERRORS, now=NOW)
    assert a["verdict"] == "PASS", [c for c in a["checks"] if not c["passed"]]
    assert a["run_id"] == b["run_id"]


def test_sim_conformance_run_cannot_paper_enable_a_venue():
    reg, p = registry(), Principal()
    reg.create_draft("k", "kraken-spot", "Kraken", "live")
    reg.add_key("k", "read", FAKE, READ)
    reg.connect_read("k", clock_skew_ms=10, capability={"x": 1}, latency_ms=50)
    from engine.common.canonical import content_hash
    reg.approve_capability("k", p.approve("CAPABILITY_APPROVE", content_hash({"x": 1})))
    with pytest.raises(VenueRefused) as e:
        reg.enable_paper("k", run_suite("kraken-spot", "0.1.0", ERRORS, now=NOW))
    assert e.value.code == "CONFORMANCE_ENVIRONMENT"


def test_lifecycle_guards():
    reg = registry()
    with pytest.raises(VenueRefused):
        reg.create_draft("x", "kraken-spot", "Kraken", "testnet")  # Kraken spot has demo, not testnet
    reg.create_draft("k", "kraken-spot", "Kraken", "live")
    with pytest.raises(VenueRefused) as e:
        reg.connect_read("k", clock_skew_ms=10, capability={}, latency_ms=1)
    assert e.value.code == "NO_READ_KEY"
    reg.add_key("k", "read", FAKE, READ)
    with pytest.raises(VenueRefused) as e:
        reg.connect_read("k", clock_skew_ms=600, capability={}, latency_ms=1)
    assert e.value.code == "CLOCK_SKEW"
    assert reg.instances["k"].keys["read"].trust == "ATTESTED"  # Kraken keys are capped at ATTESTED
    assert "api_secret" not in repr(reg.instances["k"])  # only a vault reference is kept


def test_capability_change_needs_reapproval():
    reg, p = registry(), Principal()
    v = paper_enabled(reg, p)
    assert reg.refresh_capability("kraken-main", {"changed": True})
    assert v.capability_approved_hash is None and "CAPABILITY_CHANGED" in reg.entries_blocked("kraken-main")
    from engine.common.canonical import content_hash
    reg.approve_capability("kraken-main", p.approve("CAPABILITY_APPROVE", content_hash({"changed": True})))
    assert not reg.entries_blocked("kraken-main")


def test_remove_requires_empty_and_signed():
    reg, p = registry(), Principal()
    paper_enabled(reg, p)
    from engine.common.canonical import content_hash
    ap = p.approve("VENUE_REMOVE", content_hash({"instance_id": "kraken-main"}))
    with pytest.raises(VenueRefused):
        reg.remove("kraken-main", ap, open_positions=1, open_orders=0, balance_usd=0)
    reg.remove("kraken-main", ap, open_positions=0, open_orders=0, balance_usd=1.0)
    assert reg.instances["kraken-main"].state == "REMOVED" and reg.secrets._d == {}
    assert reg.instances["kraken-main"].history  # history kept


def test_rate_lanes_protect_reserve():
    b = TokenBucket(capacity=4, refill_per_s=1.0)
    assert sum(b.try_take(Lane.DATA, 0) for _ in range(5)) == 2
    assert sum(b.try_take(Lane.ENTRY, 0) for _ in range(5)) == 1
    assert b.try_take(Lane.PROTECT, 0)
    b.update_from_venue(used=4, limit=4, now=0)
    assert not b.try_take(Lane.PROTECT, 0)
    assert b.try_take(Lane.PROTECT, 1.5)


def oms():
    v = _sim()
    o = OMS(v, instruments={INST: SPEC})
    o.reconcile()
    return v, o


def test_stop_cannot_widen_through_oms():
    v, o = oms()
    o.submit(OrderRequest("e1", INST, "buy", OrderType.IOC, 0.01, Purpose.ENTRY, price=60_010.0))
    o.set_stop(INST, 58_000.0)
    with pytest.raises(StopWidenAttempt):
        o.set_stop(INST, 57_000.0)


def test_entry_ladder_fills_as_maker_when_market_comes_back():
    v, o = oms()
    w = EntryWorker(o, INST, 0.01, bar_close_s=0, limit_max=61_000.0)
    w.step(0, 60_000.0, 60_010.0)
    v.set_market(INST, 59_980.0, 59_990.0)  # offer trades down through our bid
    w.step(30, 59_980.0, 59_990.0)
    assert w.done and abs(o.j.positions[INST] - 0.01) < 1e-12
    assert [f.liquidity for f in v.fills] == ["maker"]


def test_ladder_respects_limit_max():
    v, o = oms()
    w = EntryWorker(o, INST, 0.01, bar_close_s=0, limit_max=59_000.0)  # sizing forbids paying the current price
    w.step(901, 60_000.0, 60_010.0)
    assert w.done and not [c for c in v.calls if c.startswith("place")]


def test_dead_man_not_used_when_timer_would_cancel_stops():
    v, o = oms()
    assert DeadMan(o, spares_stops=False).refresh(0) is False and v.dead_man_deadline_ms is None


def test_dust_written_off_with_ledger_entry():
    v, o = oms()
    o.submit(OrderRequest("e1", INST, "buy", OrderType.IOC, 0.0001, Purpose.ENTRY, price=60_010.0))
    # 0.0001 BTC ~ $6 >= $5 min notional: tradable. Sell most of it leaves nothing; buy a sub-lot residual via fee-free sim
    o.j.positions[INST] = 0.00005
    o._write_off_dust(INST, 60_000.0)
    assert o.j.positions[INST] == 0.0 and o.j.ledger[-1]["kind"] == "DUST_WRITE_OFF"


def test_protective_exit_allowed_while_entries_blocked():
    v, o = oms()
    o.submit(OrderRequest("e1", INST, "buy", OrderType.IOC, 0.01, Purpose.ENTRY, price=60_010.0))
    o.incidents.open_incident("PROTECTION_UNVERIFIED", "S1", o.scope, "test")
    with pytest.raises(OrderRefused):
        o.submit(OrderRequest("e2", INST, "buy", OrderType.IOC, 0.01, Purpose.ENTRY, price=60_010.0))
    lo = o.submit(OrderRequest("x1", INST, "sell", OrderType.MARKET, 0.01, Purpose.FLATTEN))
    assert lo.status == OrderStatus.FILLED and o.j.positions[INST] == 0.0


@pytest.mark.parametrize("seed", range(12))
def test_fault_injection_fuzz_never_double_closes_or_goes_negative(seed):
    """Random prices and random faults across entries, ratchets, stream gaps and restarts. After every
    reconciliation the engine's position equals the venue balance, the position never goes negative, and any
    open position is either verifiably protected or flagged PROTECTION_UNVERIFIED."""
    rng = random.Random(seed)
    v, o = oms()
    px = 60_000.0
    faults = {"place": ["timeout_after_accept", "timeout_before_accept", "rate_limited", "reject"],
              "amend": ["fill_first", "silent_noop", "timeout"], "cancel": ["fill_first"], "stream": ["drop"]}
    for step in range(120):
        px *= 1 + rng.gauss(0, 0.004)
        v.set_market(INST, round(px, 1), round(px + 10, 1))
        if rng.random() < 0.15:
            m = rng.choice(list(faults))
            v.inject(m, rng.choice(faults[m]))
        try:
            if o.j.positions.get(INST, 0.0) <= 0 and rng.random() < 0.3:
                o.submit(OrderRequest(o.new_client_id("e"), INST, "buy", OrderType.IOC, 0.01, Purpose.ENTRY,
                                      price=round(px + 20, 1)))
                o.j.stop_targets.pop(INST, None)
                o.set_stop(INST, px * 0.95)
            elif o.j.positions.get(INST, 0.0) > 0 and INST in o.j.stop_targets:
                o.set_stop(INST, max(o.j.stop_targets[INST], px * 0.97))
        except (OrderRefused, VenueError, StopWidenAttempt):
            pass
        o.process_stream(v.events_since(o.j.last_stream_seq))
        if not o.reconciled or step % 25 == 0:
            if step % 25 == 0:
                o = OMS(v, instruments={INST: SPEC}, journal=o.j, fence=o.fence)  # restart
            rep = o.reconcile()
            assert rep.ok, rep.mismatches
        assert o.j.positions.get(INST, 0.0) >= -1e-12
        assert v.balances["BTC"] >= -1e-12
    o.reconcile()
    assert abs(o.j.positions.get(INST, 0.0) - v.balances["BTC"]) < SPEC.lot / 2
    if o.j.positions.get(INST, 0.0) > 0 and INST in o.j.stop_targets:
        assert o.verify_protection(INST) or "PROTECTION_UNVERIFIED" in o.incidents.open_codes(o.scope)


def test_shipped_connectors():
    reg = ConnectorRegistry.shipped()
    assert set(reg.types) == {"binance-spot", "binance-usdm", "bybit-v5-spot", "bybit-v5-linear", "kraken-spot",
                              "kraken-futures", "ccxt-generic"}
    assert not reg.get("ccxt-generic").trade_capable
