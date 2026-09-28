from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from engine.common.incidents import IncidentLog
from engine.evidence import backup
from engine.evidence.drills import kill_verifier_drill, replay_evidence
from engine.evidence.ledger import Ledger, LedgerUnbalanced
from engine.evidence.nav import reconcile_nav
from engine.evidence.performance import UnitFund
from engine.evidence.stress import policy_max_book, run_battery, time_to_flatten_min
from engine.evidence.verifier import Verifier, Watchdog, independent_signal
from engine.execution.conformance import INST, SPEC, _sim
from engine.execution.model import OrderRequest, OrderType, Purpose
from engine.execution.oms import OMS, OrderRefused
from engine.policy.loader import load_policy
from engine.replay.paper import ReplayConfig, replay
from engine.router.router import StrategyRouter
from engine.secrets.store import InMemorySecretStore
from engine.signal.trend import SignalParams, signal_at
from tests.helpers.fixtures import policy, universe

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def seeded():
    led = Ledger()
    led.post_deposit(venue_or_bank="kraken", ccy="USD", amount=30_000, ref="d1")
    led.post_fill(venue="kraken", fill_ref="f1", side="buy", base="BTC", quote="USD", qty=0.1, price=60_000.0, fee=24.0)
    return led


def test_ledger_balances_and_is_idempotent_per_ref():
    led = seeded()
    n = len(led.entries)
    led.post_fill(venue="kraken", fill_ref="f1", side="buy", base="BTC", quote="USD", qty=0.1, price=60_000.0, fee=24.0)
    assert len(led.entries) == n
    b = led.balances()
    assert b[("asset:kraken:BTC", "BTC")] == Decimal("0.1")
    assert b[("cash:kraken:USD", "USD")] == Decimal("23976.0")
    with pytest.raises(LedgerUnbalanced):
        led.post("bad", [("cash:kraken:USD", 1, "USD")], ref="bad", kind="FILL")


def test_ledger_chain_break_is_an_incident():
    led, inc = seeded(), IncidentLog()
    assert led.verify(inc)
    led.entries[2] = replace(led.entries[2], amount=Decimal("999"))
    assert not led.verify(inc) and "LEDGER_CHAIN_BREAK" in inc.open_codes("engine")


def test_nav_divergence_blocks_entries():
    led, inc = seeded(), IncidentLog()
    prices = {"USD": 1.0, "BTC": 60_000.0}
    ok = reconcile_nav(led, {"kraken": {"USD": 23_976.0, "BTC": 0.1}}, {}, prices, ts="t", incidents=inc)
    assert ok.verified and ok.ledger_nav == Decimal("29976.0")
    bad = reconcile_nav(led, {"kraken": {"USD": 23_900.0, "BTC": 0.1}}, {}, prices, ts="t", incidents=inc)
    assert not bad.verified
    v = _sim()
    o = OMS(v, instruments={INST: SPEC}, incidents=inc)
    o.reconcile()
    with pytest.raises(OrderRefused) as e:
        o.submit(OrderRequest("e", INST, "buy", OrderType.IOC, 0.01, Purpose.ENTRY, price=60_010.0))
    assert e.value.code == "NAV_DIVERGENCE"
    reconcile_nav(led, {"kraken": {"USD": 23_976.0, "BTC": 0.1}}, {}, prices, ts="t", incidents=inc)
    assert "NAV_DIVERGENCE" not in inc.open_codes("engine")


def test_unit_fund_twr_and_irr():
    f = UnitFund()
    f.deposit(T0, 10_000, 0)
    f.mark(T0 + timedelta(days=182), 11_000)
    f.deposit(T0 + timedelta(days=182), 10_000, 11_000)
    f.mark(T0 + timedelta(days=365), 21_000)
    assert float(f.twr) == pytest.approx(1.1 * 21_000 / 21_000 - 1, abs=1e-12)
    assert 0.0 < f.irr(T0 + timedelta(days=365)) < 0.2


def test_backup_restore_drill_and_tamper_detection():
    store = InMemorySecretStore()
    backup.ensure_key(store)
    led = seeded()
    rec = backup.restore_drill(led, {"positions": {"BTC-USD": 0.1}}, store, ts="2026-09-28T00:00:00Z")
    assert rec["passed"] and rec["entries"] == len(led.entries)
    snap = backup.snapshot(led, {}, store, ts="x")
    ct = bytearray(__import__("base64").b64decode(snap["ciphertext"]))
    ct[5] ^= 1
    with pytest.raises(backup.RestoreFailed):
        backup.restore(dict(snap, ciphertext=__import__("base64").b64encode(bytes(ct)).decode()), store)


def test_verifier_signal_matches_engine_on_fixture():
    doc = policy()
    p = SignalParams.from_policy(doc)
    s = universe()[1]
    H, L, C = list(map(float, s.h)), list(map(float, s.l)), list(map(float, s.c))
    ver = Verifier(IncidentLog())
    for i in range(p.min_history_bars - 12, len(C), 211):
        assert ver.check_signal(signal_at(s.h, s.l, s.c, i, p).T,
                                independent_signal(H, L, C, i, doc["signal"], doc["sizing"]["ewma_lambda"]), str(i))


def test_verifier_divergence_blocks_entries():
    inc = IncidentLog()
    ver = Verifier(inc)
    assert not ver.check_signal(0.61, 0.60, "bar 1")
    assert not ver.check_risk(1.0, 100.0, 90.0, 0.0075, 1_000.0, "BTC")
    o = OMS(_sim(), instruments={INST: SPEC}, incidents=inc)
    o.reconcile()
    with pytest.raises(OrderRefused) as e:
        o.submit(OrderRequest("e", INST, "buy", OrderType.IOC, 0.01, Purpose.ENTRY, price=60_010.0))
    assert e.value.code == "RECON_BREAK"


def test_watchdog_blocks_then_clears():
    inc = IncidentLog()
    wd = Watchdog(inc)
    assert not wd.check(0)  # never heard from the verifier
    wd.beat("verifier", 1)
    assert wd.check(20) and "VERIFIER_HEARTBEAT_LOST" not in inc.open_codes("engine")
    assert not wd.check(40)


def test_kill_verifier_drill_passes():
    rec = kill_verifier_drill()
    assert rec["passed"], rec


def test_attribution_to_the_penny_on_fixture_replay():
    pol, u = load_policy(), universe()
    r = replay(u, pol.doc, StrategyRouter(pol.doc), ReplayConfig(mu_q_daily=0.001))
    rec = replay_evidence(r.trades, {s.instrument_id: s for s in u}, nav0=30_000)
    assert rec["passed"] and rec["ledger_net"] == rec["attribution_net"]
    assert Decimal(rec["ledger_net"]) == Decimal(str(round(rec["replay_float_pnl"], 2)))


def test_stress_battery_book_and_determinism():
    pol = load_policy()
    b = policy_max_book(pol.doc, "T2")
    assert len(b.positions) == 4 and sum(p.notional for p in b.positions) == pytest.approx(0.30)
    assert time_to_flatten_min(b) <= pol.doc["risk"]["ttf_ceiling_min"]["A"]
    now = datetime(2026, 9, 28, tzinfo=timezone.utc)
    assert run_battery(pol.doc, pol.hash, now=now)["run_id"] == run_battery(pol.doc, pol.hash, now=now)["run_id"]
    failing = {s["id"] for s in run_battery(pol.doc, pol.hash)["scenarios"] if s["verdict"] == "FAIL"}
    assert failing == {"S4"}  # finding: a 40% venue cap cannot survive total loss of the venue within a 20% budget
