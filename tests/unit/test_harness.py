import math
from datetime import date

import numpy as np
import pytest

from research.harness import stats
from research.harness.verdicts import PromotionGovernor, VerdictRejected, allowed_mode, record_verdict, regime_authority_allowed
from research.harness.walkforward import anchored_folds, mu_q_estimate
from research.montecarlo.sigma_star import calibrate
from tests.helpers.fixtures import policy

RNG = np.random.default_rng(3)


def test_sharpe_lower_bound_separates_edge_from_noise():
    strong = RNG.normal(0.002, 0.01, 3000)  # SR ~ 3.8 annual
    null = RNG.normal(0.0, 0.01, 3000)
    assert stats.sharpe_lower_bound(strong, reps=300) > 0
    assert stats.sharpe_lower_bound(null, reps=300) < stats.sharpe_ann(null) + 1e-9


def test_min_trl_matches_appendix_b4():
    # annual SR 0.6, normal returns -> ~7.5 years of daily data (z = 1.645)
    sr_d = 0.6 / math.sqrt(365)
    r = np.random.default_rng(5).normal(sr_d * 0.01, 0.01, 200_000)
    years = stats.min_trl(r) / 365
    assert 6.5 < years < 8.5


def test_dsr_falls_with_more_trials():
    r = RNG.normal(0.0008, 0.01, 3000)
    assert stats.deflated_sharpe(r, 1) > stats.deflated_sharpe(r, 40)


def test_n_eff_formula():
    assert stats.n_eff(216, 4, 0.7) == pytest.approx(216 / 3.1)


def test_walkforward_folds_respect_embargo_and_mu_q_shrinks():
    folds = anchored_folds(10_000, first_test_bar=3000, test_bars=2190, embargo_bars=1080)
    assert all(f.test_start_bar - f.train_end_bar == 1080 for f in folds)
    c = np.exp(np.cumsum(np.full(4000, 0.001)))
    T = np.ones(4000)
    B = np.ones(4000)
    few = mu_q_estimate(c, T, B, 400, T_entry=0.5, n0=252, q=0.25)
    many = mu_q_estimate(c, T, B, 4000, T_entry=0.5, n0=252, q=0.25)
    assert many > few


@pytest.mark.invariant("INV-28")
def test_step_without_run_id_is_not_run_and_negative_ci_cannot_pass():
    assert record_verdict(2, "A_long", "PASS", None, "sharpe", 0.5, (0.1, None)).verdict == "NOT_RUN"
    with pytest.raises(VerdictRejected):
        record_verdict(2, "A_long", "PASS", "bt-1", "sharpe", 0.4, (-0.31, None))
    with pytest.raises(VerdictRejected):
        record_verdict(2, "A_long", "PASS", "bt-1", "sharpe", 0.4, (0.0, None))
    assert record_verdict(2, "A_long", "PASS", "bt-1", "sharpe", 0.4, (0.05, None)).verdict == "PASS"


@pytest.mark.invariant("INV-29")
def test_step6_never_gates_shadow():
    recs = [record_verdict(s, "A_long", "PASS", f"bt-{s}", "m", 1.0, (0.1, None)) for s in (1, 2, 3, 4, 5, 7, 8)]
    assert allowed_mode(recs) == "SHADOW"
    recs.append(record_verdict(6, "A_long", "FAIL", "bt-6", "ece", 0.09, (None, None)))
    assert allowed_mode(recs) == "SHADOW" and regime_authority_allowed(recs) == "T0"
    assert allowed_mode(recs[:-2]) == "PAPER"  # missing step 8 blocks SHADOW


@pytest.mark.invariant("INV-20")
def test_live_horizon_cannot_be_evidence_of_edge():
    with pytest.raises(VerdictRejected, match="offline replay"):
        record_verdict(2, "A_long", "PASS", "live-30d", "sharpe", 2.5, (0.8, None), source="live")


@pytest.mark.invariant("INV-27")
def test_second_promotion_in_quarter_rejected():
    g = PromotionGovernor(per_sleeve_per_quarter=1, programme_per_year=8)
    g.promote("A_long", date(2027, 1, 10))
    with pytest.raises(VerdictRejected):
        g.promote("A_long", date(2027, 3, 1))
    g.promote("A_long", date(2027, 4, 2))
    for m in range(5, 11):
        g.promote(f"S{m}", date(2027, m, 1))
    with pytest.raises(VerdictRejected, match="programme"):
        g.promote("B_short", date(2027, 11, 1))


def test_monte_carlo_ladder_must_matter():
    r = RNG.normal(0.0002, 0.006, 3000)
    mc = calibrate(r, policy()["risk"]["ladder"], p_max=0.05, dd_limit=0.20, paths=400, grid=(0.06, 0.12, 0.2))
    assert mc.control_ok
    assert all(off >= on for _g, on, off, _f in mc.grid)
    assert mc.grid[-1][2] > mc.grid[-1][1]  # ladder off gives a larger P(DD >= 20%)
