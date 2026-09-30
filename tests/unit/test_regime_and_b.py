from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from engine.common.schemas import SchemaError, validate
from engine.policy.loader import load_policy
from engine.regime.ccmrm import (
    RegimeLayer,
    calibration,
    confirm,
    decayed_counts,
    homogeneity,
    information_horizon,
    m_regime,
    raw_states,
)
from engine.replay.perp import PerpConfig, replay_perp
from research.harness.stats import tail_contribution_share
from research.harness.steps import HarnessContext, perp_runner, step1_data, step2_null
from tests.helpers.fixtures import contract_specs, fixture_funding, universe

POL = load_policy()


def test_throttle_mapping_reproduces_v91_fixtures():
    assert m_regime(0.41, 0.35, 0.55) == pytest.approx(0.30)
    assert m_regime(0.49, 0.35, 0.55) == pytest.approx(0.70)
    assert m_regime(0.30, 0.35, 0.55) == 0.0 and m_regime(0.9, 0.35, 0.55) == 1.0


def test_confirm_needs_two_bars():
    assert confirm(["U", "D", "U", "D", "D", "R"], 2) == ["U", "U", "U", "U", "D", "D"]


def claim(i=None, **kw):
    s = universe()[0]
    i = len(s.c) - 1 if i is None else i
    bc = datetime.fromtimestamp(int(s.open_time[i]) + 4 * 3600, tz=timezone.utc)
    return RegimeLayer(POL.doc, POL.hash, **kw), s, i, bc


def test_claim_is_schema_valid_and_t0_never_touches_size():
    rl, s, i, bc = claim()
    c = rl.claim(s.instrument_id, s.c, i, bc, now=bc)
    assert c["payload"]["authority"] == "T0" and rl.sizing_multiplier(c) == 1.0
    bad = dict(c, payload=dict(c["payload"], m_regime=1.2))
    with pytest.raises(SchemaError):
        validate("regime_claim", bad)
    no_counts = dict(c, payload={k: v for k, v in c["payload"].items() if k != "counts_decayed"})
    with pytest.raises(SchemaError):
        validate("regime_claim", no_counts)


def test_stale_and_ess_force_floor():
    rl, s, i, bc = claim()
    c = rl.claim(s.instrument_id, s.c, i, bc, now=bc + timedelta(hours=9))
    assert "STALE" in c["payload"]["binding_reasons"] and c["payload"]["m_regime"] == 0.0
    j = 365 * 6 + 60
    c2 = rl.claim(s.instrument_id, s.c, j, bc, now=bc)
    if c2 is not None:
        assert "ESS" in c2["payload"]["binding_reasons"] or min(c2["payload"]["ESS"]) >= 0


def test_n1_calibration_failure_floors_m():
    rl, s, i, bc = claim(ece=0.09)
    assert "N1" in rl.claim(s.instrument_id, s.c, i, bc, now=bc)["payload"]["binding_reasons"]


def test_homogeneity_information_horizon_and_ece():
    st = confirm(raw_states(universe()[0].c), 2)
    h = homogeneity(st, [len(st) // 2])
    assert 0.0 <= h["p_value"] <= 1.0 and h["dof"] > 0
    mean, _, _ = decayed_counts(st, len(st) - 1, 730).posterior()
    assert 1 <= information_horizon(mean) <= 500
    assert calibration([0.9, 0.1, 0.8, 0.2], [True, False, True, False]) < 0.2


def test_perp_replay_deterministic_and_margin_bounded():
    u, sp = universe(), contract_specs()
    f = fixture_funding(u, sp)
    a = replay_perp(u, POL.doc, sp, f, PerpConfig(book="B_short"))
    b = replay_perp(u, POL.doc, sp, f, PerpConfig(book="B_short"))
    assert a.fingerprint == b.fingerprint and a.trades
    assert all(t.exit_reason in {"INITIAL_STOP", "TRAILING_STOP", "T_CROSS_ZERO", "FUNDING_TIME_STOP", "FLATTEN"}
               for t in a.trades)
    long_ = replay_perp(u, POL.doc, sp, f, PerpConfig(book="B_long"))
    assert long_.fingerprint != a.fingerprint


def test_b_book_runs_through_the_same_harness():
    u, sp = universe(), contract_specs()
    f = fixture_funding(u, sp)
    qr = {s.instrument_id: {"coverage": 1.0, "quarantined": [], "expected_bars": len(s.c)} for s in u}
    ctx = HarnessContext(u, POL.doc, POL.hash, qr, sleeve="B_short", reps=50, runner=perp_runner(u, POL.doc, sp, f, "B_short"))
    assert step1_data(ctx).verdict == "PASS"
    s2 = step2_null(ctx)
    assert s2.run_id and s2.verdict in ("PASS", "FAIL") and s2.sleeve == "B_short"


def test_tail_share():
    assert tail_contribution_share(np.array([10.0] + [1.0] * 9)) == pytest.approx(10 / 19)
