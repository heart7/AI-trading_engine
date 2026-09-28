"""Negative tests for the P4 invariants (spec §18): INV-14, 15, 17, 18, 19, 22, 23, 24, 25."""
import re
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from engine.admissibility.ttf import admit_b_universe, admit_by_ttf
from engine.common.incidents import IncidentLog
from engine.strategy_b.adl import ADLInTrainingSet, adl_controls, exclude, record_adl_close, training_set
from engine.strategy_b.contracts import ContractRegistry
from engine.strategy_b.funding import (
    OutcomeWeightedHold,
    UnconditionalFunding,
    conditional_estimate,
    perp_cost_gate,
    unconditional_estimate,
)
from engine.strategy_b.margin import MarginInvariant, PerpOrder, PerpPosition, check_order, liquidation_distance, max_leverage
from research.harness.stats import WrongDeflation, n_eff_for_test
from research.registry.registry import GuardrailMissing, register
from tests.helpers.fixtures import contract_specs, policy
from tests.helpers.venues import Principal

ROOT = Path(__file__).resolve().parents[2]
POL = policy()
SPECS = contract_specs()


@pytest.mark.invariant("INV-14")
def test_order_breaching_simultaneous_liquidation_cap_refused():
    # cap = phi 0.5 x hard DD 0.20 = 10% NAV of posted margin
    book = [PerpPosition("FIXTURE_BTC", -1, 0.20, 2.5), PerpPosition("FIXTURE_ETH", -1, 0.12, 2.5)]  # 8% + 4.8%
    with pytest.raises(MarginInvariant):
        check_order(POL, book[:1], PerpOrder("FIXTURE_ETH", -1, 0.12, 2.5))
    check_order(POL, [], PerpOrder("FIXTURE_ETH", -1, 0.12, 2.5))
    assert book


@pytest.mark.invariant("INV-14")
def test_leverage_respects_liquidation_buffer():
    lev = max_leverage(POL, stop_distance=0.08, mmr=0.005)
    assert liquidation_distance(lev, 0.005) >= max(3 * 0.08, 0.20) - 1e-12 and lev <= 3.0


@pytest.mark.invariant("INV-15")
def test_cross_margin_has_no_code_path():
    with pytest.raises(ValueError):
        PerpOrder("FIXTURE_BTC", 1, 0.1, 2.0, margin_mode="CROSS")
    src = "\n".join(p.read_text() for p in (ROOT / "engine").rglob("*.py"))
    assert not re.search(r"margin_mode\s*=\s*['\"]CROSS", src)


def _rates(n=500, seed=1):
    rng = np.random.default_rng(seed)
    return rng.normal(0.0002, 0.0001, n), rng.random(n) < 0.5


@pytest.mark.invariant("INV-17")
def test_unconditional_funding_rejected_by_cost_gate():
    r, state = _rates()
    un = unconditional_estimate("FIXTURE_BTC", "B_short", r, SPECS["FIXTURE_BTC"])
    hold = OutcomeWeightedHold(0.4, 20, 8)
    with pytest.raises(UnconditionalFunding):
        perp_cost_gate(e_gross=0.1, certain_cost=0.002, funding=un, hold=hold, k_cost=2, lam=1)
    cond = conditional_estimate("FIXTURE_BTC", "B_short", r, state, SPECS["FIXTURE_BTC"])
    assert perp_cost_gate(e_gross=0.1, certain_cost=0.002, funding=cond, hold=hold, k_cost=2, lam=1)["pass"] in (True, False)


@pytest.mark.invariant("INV-17")
def test_short_book_never_assumes_favourable_carry_before_measuring():
    est = conditional_estimate("FIXTURE_BTC", "B_short", [0.001] * 20, [True] * 20, SPECS["FIXTURE_BTC"])
    assert est.assumed and est.paid_per_day == 0.0  # shorts would *receive* here; the default refuses to count it


@pytest.mark.invariant("INV-18")
def test_hold_must_be_outcome_weighted():
    r, state = _rates()
    cond = conditional_estimate("FIXTURE_BTC", "B_short", r, state, SPECS["FIXTURE_BTC"])
    with pytest.raises(TypeError):
        perp_cost_gate(e_gross=0.1, certain_cost=0.002, funding=cond, hold=12.0, k_cost=2, lam=1)


@pytest.mark.invariant("INV-18")
def test_time_stop_change_without_tail_guardrail_rejected():
    h = {"id": "H-B-FTS-TIGHTEN", "owner": "principal", "sleeve": "B_short", "harness_step": 4, "class": "HYPOTHESIS",
         "description": "Tighten the funding time stop", "metric": "net Sharpe", "mde": {"sharpe": 0.2},
         "pass_rule": "x", "falsification_rule": "y", "budget_debit": 1, "run_ids": [], "status": "PRE_REGISTERED",
         "registered_at": "2026-10-01", "changes": ["perps.funding_time_stop_R.reduce"]}
    with pytest.raises(GuardrailMissing):
        register([], h, budget_per_year=40)
    assert register([], dict(h, guardrails=["tail_contribution_share"]), budget_per_year=40)


@pytest.mark.invariant("INV-19")
def test_paired_test_with_return_correlation_rejected():
    with pytest.raises(WrongDeflation):
        n_eff_for_test(1000, 4, test="paired", rho=0.6, rho_kind="rho_return")
    assert n_eff_for_test(1000, 4, test="paired", rho=0.4, rho_kind="rho_paired") == pytest.approx(1000 / 2.2)


@pytest.mark.invariant("INV-22")
def test_adl_episode_is_incident_and_never_trains():
    inc = IncidentLog()
    ep = record_adl_close(inc, {"episode_id": "e9", "pnl": -12.0}, "paper-perp")
    assert "ADL_EVENT" in inc.open_codes("venue:paper-perp")
    with pytest.raises(ADLInTrainingSet):
        training_set([{"episode_id": "e1"}, ep])
    assert exclude([{"episode_id": "e1"}, ep]) == [{"episode_id": "e1"}]


def test_adl_queue_controls():
    s = adl_controls(POL, {"a": "RISING", "b": "LOW"})
    assert s.new_exposure_mult == 0.5 and not s.reduce_only
    assert adl_controls(POL, {"a": "HIGH", "b": "HIGH"}).reduce_only


@pytest.mark.invariant("INV-23")
def test_instrument_pushing_portfolio_over_ttf_ceiling_not_admissible():
    vol = {"BTC": 2e6, "THIN": 2e4}
    ok = admit_by_ttf({"BTC": 1e6}, vol, "BTC", 2e5, 30)
    assert ok.admissible
    bad = admit_by_ttf({"BTC": 1e6}, vol, "THIN", 2e4, 30)  # 2e4 / (2e4 x 0.03) = 33 min
    assert not bad.admissible and bad.code == "NOT_ADMISSIBLE"
    assert not admit_b_universe(POL, vol_usd_30d_median=5e7, oi_usd=1e8, history_days=400, spec_approved=True).admissible


@pytest.mark.invariant("INV-24")
def test_no_hard_coded_funding_interval_in_code():
    pattern = re.compile(r"28800|\b8h\b|8 \* 3600|funding_interval_h\s*=\s*\d")
    hits = [f"{p.relative_to(ROOT)}:{n}" for base in ("engine", "research", "tools") for p in (ROOT / base).rglob("*.py")
            for n, line in enumerate(p.read_text().splitlines(), 1) if pattern.search(line)]
    assert hits == []


@pytest.mark.invariant("INV-24")
def test_funding_rate_scales_with_the_spec_interval():
    r, state = [0.0001] * 300, [True] * 300
    e8 = conditional_estimate("x", "B_long", r, state, SPECS["FIXTURE_BTC"])  # interval from data
    e1 = conditional_estimate("x", "B_long", r, state, SPECS["FIXTURE_SOL"])
    assert e1.paid_per_day == pytest.approx(e8.paid_per_day * SPECS["FIXTURE_BTC"].funding_interval_h
                                            / SPECS["FIXTURE_SOL"].funding_interval_h)


@pytest.mark.invariant("INV-25")
def test_mm_schedule_change_blocks_entries_and_recomputes_liquidation():
    inc, p = IncidentLog(), Principal()
    reg = ContractRegistry(inc)
    spec = SPECS["FIXTURE_BTC"]
    reg.update(spec)
    reg.approve(spec.instrument_id, p.approve("CAPABILITY_APPROVE", spec.hash))
    assert reg.entries_allowed(spec.instrument_id) == (True, "OK")
    new = replace(spec, valid_from="2026-10-01T00:00:00Z", mm_schedule=((1e6, 0.01), (1e12, 0.02)))
    out = reg.update(new, recompute=lambda s: {"pos-1": liquidation_distance(2.5, s.mmr(5e4))})
    assert out["material_change"] == ["mm_schedule"] and out["liquidation_distances"]["pos-1"] == pytest.approx(0.39)
    assert reg.entries_allowed(spec.instrument_id) == (False, "CONTRACT_SPEC_UNAPPROVED")
    assert "CONTRACT_SPEC_CHANGED" in inc.open_codes(f"instrument:{spec.instrument_id}")
    reg.approve(spec.instrument_id, p.approve("CAPABILITY_APPROVE", new.hash))
    assert reg.entries_allowed(spec.instrument_id)[0]


def test_tick_only_change_needs_no_reapproval():
    inc, p = IncidentLog(), Principal()
    reg = ContractRegistry(inc)
    spec = SPECS["FIXTURE_ETH"]
    reg.update(spec)
    reg.approve(spec.instrument_id, p.approve("CAPABILITY_APPROVE", spec.hash))
    reg.update(replace(spec, tick=0.05))
    assert reg.entries_allowed(spec.instrument_id)[0]
