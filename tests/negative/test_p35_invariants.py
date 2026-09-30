"""Negative tests for the P3.5 evidence-plane invariants (spec §18): INV-16, 26, 41, 42."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from engine.common.incidents import IncidentLog
from engine.evidence.attribution import Episode, attribute
from engine.evidence.collateral import check_collateral
from engine.evidence.ledger import Ledger
from engine.evidence.performance import UnitFund
from engine.evidence.stress import run_battery
from engine.execution.conformance import INST, SPEC, _sim
from engine.execution.model import OrderRequest, OrderType, Purpose
from engine.execution.oms import OMS, OrderRefused
from engine.governance.proposals import ProposalRefused, propose
from engine.policy.loader import load_policy, policy_from_doc
from tests.helpers.fixtures import policy

T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)


def collateral(**kw):
    base = dict(nav=30_000, venue_holdings_usd={"kraken": {"BTC": 6_000}}, venue_cash_usd={"kraken": 3_000},
                stable_holdings_usd={}, stable_prices={}, offvenue_usd=21_000)
    base.update(kw)
    return base


@pytest.mark.invariant("INV-16")
def test_venue_cap_breach_blocks_entries_on_that_venue():
    inc = IncidentLog()
    rep = check_collateral(policy(), **collateral(venue_holdings_usd={"sim-kraken-spot": {"BTC": 11_000}},
                                                  venue_cash_usd={"sim-kraken-spot": 3_000}), incidents=inc)
    assert "sim-kraken-spot" in rep.blocked_venues
    v = _sim()
    o = OMS(v, instruments={INST: SPEC}, incidents=inc)
    o.reconcile()
    with pytest.raises(OrderRefused) as e:
        o.submit(OrderRequest("e1", INST, "buy", OrderType.IOC, 0.01, Purpose.ENTRY, price=60_010.0))
    assert e.value.code == "COLLATERAL_CAP"


@pytest.mark.invariant("INV-16")
def test_issuer_cap_par_band_and_reserve_are_hard_checks():
    rep = check_collateral(policy(), **collateral(stable_holdings_usd={"USDC": 9_000}, stable_prices={"USDC": 0.99},
                                                  offvenue_usd=3_000))
    assert any("ISSUER_CAP" in b for b in rep.engine_blocks)
    assert any("OFF_EXCHANGE_RESERVE" in b for b in rep.engine_blocks)
    assert rep.reduce_only_stables == ["USDC"]


@pytest.mark.invariant("INV-16")
def test_sweeps_are_human_executed_transfer_intents():
    rep = check_collateral(policy(), **collateral(venue_cash_usd={"kraken": 9_000}))
    assert rep.sweeps and all(s.executed_by == "human" for s in rep.sweeps)
    assert rep.sweeps[0].amount == 6_000


@pytest.mark.invariant("INV-26")
def test_attribution_sums_exactly_with_no_residual():
    eps = [Episode.of(episode_id="1", sleeve="A_long", qty=0.0123, entry_decision=60_000.1, entry_model=60_030.2,
                      entry_fill=60_041.7, exit_decision=66_010.3, exit_model=65_977.9, exit_fill=65_960.05,
                      fees=4.37, tax_reserve=1.11, factor_return=0.071),
           Episode.of(episode_id="2", sleeve="A_long", qty=150.0, entry_decision=0.61, entry_model=0.6103,
                      entry_fill=0.6101, exit_decision=0.55, exit_model=0.5497, exit_fill=0.5492, fees=0.61,
                      factor_return=-0.02, process_flags=("STALE_INPUT",))]
    a = attribute(eps)
    assert a.exact and "RESIDUAL" not in a.by_component and "DIRECTION" not in a.by_component
    assert a.by_component["BETA"] + a.by_component["ALPHA"] == a.net
    assert set(a.by_process) == {"OUTCOME_VARIANCE", "PROCESS_ERROR:data"}


@pytest.mark.invariant("INV-26")
def test_attribution_mismatch_with_ledger_opens_incident():
    inc = IncidentLog()
    e = Episode.of(episode_id="1", sleeve="A_long", qty=1, entry_decision=100, entry_model=100, entry_fill=100,
                   exit_decision=110, exit_model=110, exit_fill=110, fees=1)
    assert not attribute([e], ledger_net=Decimal("9.50"), incidents=inc).exact
    assert "ATTRIBUTION_MISMATCH" in inc.open_codes("engine")


@pytest.mark.invariant("INV-41")
def test_deposit_is_not_pnl_and_leaves_drawdown_unchanged():
    led = Ledger()
    led.post_deposit(venue_or_bank="bank", ccy="USD", amount=30_000, ref="d1")
    f = UnitFund()
    f.deposit(T0, 30_000, 0)
    f.mark(T0 + timedelta(days=10), 27_000)  # -10%
    dd, uv = f.drawdown, f.unit_value
    f.deposit(T0 + timedelta(days=11), 50_000, nav_before=27_000)
    led.post_deposit(venue_or_bank="bank", ccy="USD", amount=50_000, ref="d2")
    assert f.drawdown == dd and f.unit_value == uv
    assert not [e for e in led.entries if e.account.startswith("pnl:")]
    f.withdraw(T0 + timedelta(days=12), 20_000, nav_before=77_000)
    assert f.drawdown == dd  # a withdrawal does not create one either


@pytest.mark.invariant("INV-42")
def test_policy_proposal_needs_stress_pass_for_its_own_hash():
    cur = load_policy()
    doc = dict(cur.doc, risk=dict(cur.doc["risk"], venue_exposure_max=0.15))
    new = policy_from_doc(doc)
    with pytest.raises(ProposalRefused) as e:
        propose(cur, new, None)
    assert e.value.reason == "STRESS_BATTERY_MISSING"
    with pytest.raises(ProposalRefused) as e:
        propose(cur, new, run_battery(cur.doc, cur.hash))
    assert e.value.reason == "STRESS_BATTERY_STALE"
    run = run_battery(new.doc, new.hash)
    assert run["verdict"] == "PASS"
    assert propose(cur, new, run).stress_run_id == run["run_id"]


@pytest.mark.invariant("INV-42")
def test_current_policy_fails_s4_so_any_proposal_of_it_is_refused():
    cur = load_policy()
    doc = dict(cur.doc, operating_costs_usd_month=250)
    new = policy_from_doc(doc)
    run = run_battery(new.doc, new.hash)
    s4 = next(s for s in run["scenarios"] if s["id"] == "S4")
    assert s4["verdict"] == "FAIL" and s4["loss"] == pytest.approx(0.40)
    with pytest.raises(ProposalRefused) as e:
        propose(cur, new, run)
    assert e.value.reason == "STRESS_BATTERY_FAIL" and "S4" in e.value.detail

