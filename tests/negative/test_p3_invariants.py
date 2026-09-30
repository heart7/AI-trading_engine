"""Negative tests for the P3 execution-plane invariants (spec §18): INV-02, 30, 31, 35-40, 43."""
from datetime import timedelta

import pytest

from engine.execution.conformance import INST, SPEC, _sim
from engine.execution.ladder import EntryWorker
from engine.execution.model import OrderRequest, OrderStatus, OrderType, Purpose
from engine.execution.oms import OMS, Fence, Journal, OrderRefused
from engine.execution.venues import PermissionProbe, VenueRefused
from tests.helpers.venues import FAKE, NOW, TRADE, Principal, paper_enabled, registry, trade_ready

WITHDRAW = PermissionProbe(read=True, trade=True, withdraw=True)


def oms(v=None, **kw):
    v = v or _sim()
    o = OMS(v, instruments={INST: SPEC}, **kw)
    o.reconcile()
    return v, o


def buy(o, qty=0.01, px=60_010.0, type_=OrderType.IOC, purpose=Purpose.ENTRY):
    return o.submit(OrderRequest(o.new_client_id("t"), INST, "buy", type_, qty, purpose, price=px))


@pytest.mark.invariant("INV-02")
def test_withdrawal_key_refused_and_never_stored():
    reg = registry()
    reg.create_draft("k", "kraken-spot", "Kraken", "live")
    with pytest.raises(VenueRefused) as e:
        reg.add_key("k", "trade", FAKE, WITHDRAW)
    assert e.value.code == "WITHDRAWAL_PERMISSION"
    assert reg.secrets._d == {} and "trade" not in reg.instances["k"].keys


@pytest.mark.invariant("INV-30")
def test_entry_can_never_be_market():
    _, o = oms()
    with pytest.raises(OrderRefused) as e:
        buy(o, type_=OrderType.MARKET, px=None)
    assert e.value.code == "ENTRY_MUST_BE_LIMIT"


@pytest.mark.invariant("INV-30")
def test_entry_ladder_escalates_to_ioc_limit_only():
    v, o = oms()
    w = EntryWorker(o, INST, 0.01, bar_close_s=0, limit_max=61_000.0)
    for t in range(0, 15 * 60 + 1, 30):
        v.set_market(INST, 60_000.0 + t, 60_010.0 + t)  # price runs away: post-only never fills
        w.step(t, 60_000.0 + t, 60_010.0 + t)
    types = {c.split(":")[2] for c in v.calls if c.startswith("place:")}
    assert types <= {"POST_ONLY", "IOC"} and "IOC" in types
    last = o.j.orders[w.history[-1]]
    assert last.req.type == OrderType.IOC and last.req.price is not None and last.req.price <= 61_000.0


@pytest.mark.invariant("INV-31")
def test_protection_readback_mismatch_blocks_entries():
    v, o = oms()
    buy(o)
    assert o.set_stop(INST, 57_000.0)
    v.inject("amend", "silent_noop")  # venue acks the ratchet but leaves the old stop price
    assert not o.set_stop(INST, 58_000.0)
    assert "PROTECTION_UNVERIFIED" in o.incidents.open_codes(o.scope)
    with pytest.raises(OrderRefused) as e:
        buy(o, 0.001)
    assert e.value.code == "PROTECTION_UNVERIFIED"
    # protective orders still go through while entries are blocked
    o.set_stop(INST, 58_000.0)
    assert o.verify_protection(INST) and "PROTECTION_UNVERIFIED" not in o.incidents.open_codes(o.scope)


@pytest.mark.invariant("INV-31")
def test_every_entry_fill_is_protected_at_filled_qty():
    v, o = oms()
    v.set_market(INST, 60_000.0, 60_010.0, depth=0.004)
    buy(o, 0.01)
    o.j.stop_targets[INST] = 57_000.0
    assert o.on_entry_fills(INST)
    assert abs(v.query(o.j.stops[INST]).qty - 0.004) < 1e-12


@pytest.mark.invariant("INV-35")
def test_order_to_paper_enabled_venue_rejected():
    reg, p = registry(), Principal()
    paper_enabled(reg, p)

    class LiveLike:  # a real connector object for the same instance
        venue_id, paper, supports_amend = "kraken-main", False, True

        def snapshot(self):
            return _sim().snapshot()

        def fills_since(self, seq):
            return []

        def place(self, req):
            raise AssertionError("must not reach the venue")

    o = OMS(LiveLike(), instruments={INST: SPEC}, mode="LIVE", venues=reg, journal=Journal(positions={}))
    o.reconciled = True
    with pytest.raises(OrderRefused) as e:
        buy(o)
    assert e.value.code == "VENUE_NOT_TRADE_ENABLED"


@pytest.mark.invariant("INV-35")
def test_trade_enable_needs_signed_approval_for_this_request():
    reg, p = registry(), Principal()
    v, approval = trade_ready(reg, p)
    wrong = p.approve("VENUE_TRADE_ENABLE", "b" * 64)
    with pytest.raises(VenueRefused) as e:
        reg.enable_trade("kraken-main", product="spot", sleeve="A_long", policy_hash="a" * 64, approval=wrong)
    assert e.value.code == "APPROVAL_MISMATCH"
    reg.enable_trade("kraken-main", product="spot", sleeve="A_long", policy_hash="a" * 64, approval=approval)
    assert v.state == "TRADE_ENABLED" and reg.can_trade("kraken-main", "spot") == (True, "OK")


def test_paper_mode_refuses_real_adapters():
    class Real:
        paper = False
    with pytest.raises(OrderRefused) as e:
        OMS(Real(), instruments={})
    assert e.value.code == "PAPER_NEVER_SENDS"


@pytest.mark.invariant("INV-36")
def test_reprobe_finding_withdrawal_suspends_with_s1():
    reg, p = registry(), Principal()
    paper_enabled(reg, p)
    reg.reprobe("kraken-main", "read", WITHDRAW)
    v = reg.instances["kraken-main"]
    assert v.state == "SUSPENDED"
    inc = [i for i in reg.incidents.items if i.code == "VENUE_PERMISSION_VIOLATION"]
    assert inc and inc[0].severity == "S1"


@pytest.mark.invariant("INV-37")
def test_read_slot_refuses_trade_capable_key():
    reg = registry()
    reg.create_draft("k", "kraken-spot", "Kraken", "live")
    with pytest.raises(VenueRefused) as e:
        reg.add_key("k", "read", FAKE, TRADE)
    assert e.value.code == "READ_SLOT_TRADE_CAPABLE"


@pytest.mark.invariant("INV-38")
def test_ccxt_generic_never_trade_enabled():
    reg, p = registry(), Principal()
    reg.create_draft("c", "ccxt-generic", "generic", "live")
    with pytest.raises(VenueRefused):
        reg.add_key("c", "trade", FAKE, TRADE)
    with pytest.raises(VenueRefused) as e:
        reg.enable_trade("c", product="spot", sleeve="A_long", policy_hash="a" * 64,
                         approval=p.approve("VENUE_TRADE_ENABLE", "c" * 64))
    assert e.value.code == "CONNECTOR_NEVER_TRADES"


@pytest.mark.invariant("INV-39")
def test_no_order_before_reconciliation_and_only_fenced_leader_sends():
    v = _sim()
    fence = Fence()
    o = OMS(v, instruments={INST: SPEC}, fence=fence)
    with pytest.raises(OrderRefused) as e:
        buy(o)
    assert e.value.code == "NOT_RECONCILED" and not [c for c in v.calls if c.startswith("place")]
    o.reconcile()
    buy(o)
    standby = OMS(v, instruments={INST: SPEC}, fence=fence, journal=o.j)  # failover: standby takes the lease
    standby.reconcile()
    with pytest.raises(OrderRefused) as e:
        buy(o, 0.001)
    assert e.value.code == "NOT_LEADER"
    buy(standby, 0.001)


@pytest.mark.invariant("INV-39")
def test_reconciliation_mismatch_blocks_until_resolved():
    v = _sim()
    v.balances["BTC"] = 0.5  # the venue holds coins the engine's journal does not know about
    o = OMS(v, instruments={INST: SPEC})
    rep = o.reconcile()
    assert not rep.ok and "ORDER_STATE_MISMATCH" in o.incidents.open_codes(o.scope)
    with pytest.raises(OrderRefused):
        buy(o)
    assert o.reconcile(base_holdings={"BTC": 0.5}).ok  # principal declares the pre-existing holding


@pytest.mark.invariant("INV-40")
@pytest.mark.parametrize("fault", ["timeout_after_accept", "timeout_before_accept", "ambiguous"])
def test_unknown_state_goes_to_reconciliation_never_resubmitted(fault):
    v, o = oms()
    v.inject("place", fault)
    lo = buy(o)
    assert lo.status == OrderStatus.UNKNOWN and not o.reconciled
    assert sum(c.startswith(f"place:{lo.req.client_id}") for c in v.calls) == 1
    assert o.reconcile().ok
    expect = OrderStatus.FILLED if fault == "timeout_after_accept" else OrderStatus.CANCELLED
    assert lo.status == expect


@pytest.mark.invariant("INV-43")
def test_access_record_with_other_residence_refused():
    from tests.helpers.venues import access_record
    reg, p = registry(), Principal()
    paper_enabled(reg, p)
    rec = access_record(residence="NG")
    with pytest.raises(VenueRefused) as e:
        reg.attach_access_record("kraken-main", rec, p.approve("ACCESS_RECORD", rec.subject_hash()))
    assert e.value.code == "RESIDENCE_MISMATCH"


@pytest.mark.invariant("INV-43")
def test_binance_without_venue_confirmation_cannot_trade():
    from tests.helpers.venues import access_record
    reg, p = registry(), Principal()
    rec = access_record(venue="binance", evidence=({"kind": "legal_memo", "ref": "memo.pdf"},))
    reg.venues_policy = dict(reg.venues_policy, execution_allowed={"A_long": ["binance-spot"]})
    v = paper_enabled(reg, p, "bn", "binance-spot")
    reg.add_key("bn", "trade", FAKE, PermissionProbe(read=True, trade=True, withdraw=False))
    reg.attach_access_record("bn", rec, p.approve("ACCESS_RECORD", rec.subject_hash()))
    v.caps, v.ip_allowlist_configured = {"exposure_max": 0.4}, True
    reg.geo.observe("GB", reg.incidents)
    with pytest.raises(VenueRefused) as e:
        reg.enable_trade("bn", product="spot", sleeve="A_long", policy_hash="a" * 64,
                         approval=p.approve("VENUE_TRADE_ENABLE", "d" * 64))
    assert e.value.code == "ACCESS_RECORD_INVALID" and "confirmation" in e.value.detail


@pytest.mark.invariant("INV-43")
def test_egress_outside_declared_residence_blocks_all_trading():
    reg, p = registry(), Principal()
    v, approval = trade_ready(reg, p)
    reg.enable_trade("kraken-main", product="spot", sleeve="A_long", policy_hash="a" * 64, approval=approval)
    reg.geo.observe("NG", reg.incidents)
    assert reg.can_trade("kraken-main", "spot") == (False, "GEO_EGRESS_MISMATCH")
    assert reg.execution_venues("A_long") == []


def test_access_record_expiry_suspends_venue():
    reg, p = registry(), Principal()
    v, approval = trade_ready(reg, p)
    reg.enable_trade("kraken-main", product="spot", sleeve="A_long", policy_hash="a" * 64, approval=approval)
    reg.now = lambda: NOW + timedelta(days=301)
    reg.daily_checks()
    assert v.state == "SUSPENDED" and "ACCESS_RECORD_EXPIRED" in reg.incidents.open_codes("venue:kraken-main")
