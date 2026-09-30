"""Negative tests for the P2 decision-core invariants (spec §18): INV-03..13."""
import inspect
from datetime import date, datetime, timedelta, timezone

import pytest

from engine.common.schemas import SchemaError, validate
from engine.governance import approvals as ap
from engine.portfolio.clock import IntradayTriggerIgnored, decision_cycle_guard
from engine.replay.paper import ReplayConfig, replay
from engine.risk.gates import AddRejected, EntryContext, check_add, evaluate_entry, evaluate_exit
from engine.router.router import RouterState, StrategyRouter, TierEvidence, TierRequestRefused, eligibility
from engine.sizing import cost as costmod
from engine.sizing.cost import CostInput, cost_gate
from engine.sizing.sizing import Holding, SizingInputs, compute_size
from engine.stops.stops import StopWidenAttempt, open_stop
from tests.helpers.authenticator import Authenticator
from tests.helpers.fixtures import policy, universe

NOW = datetime(2026, 9, 28, 16, 0, tzinfo=timezone.utc)
POL = policy()


def ctx(**kw):
    base = dict(engine_running=True, sleeve_allowed=True, data_fresh=True, data_certified=True, signal_abstain=None,
                admissible=True, B=1 / 3, T=0.6, T_entry=0.5, side=1, evidence_on_file=True, cost_gate=None,
                cost_R=0.1, cost_R_max=0.15, risk_budget_ok=True)
    base.update(kw)
    return EntryContext(**base)


def sizing(**kw):
    base = dict(nav=30_000, r_tier=0.0075, d=0.06, sigma_daily=0.03, mu_q_daily=0.001, kelly_k=0.25, es_mult=1.5,
                rho_stress=0.85, depth_50bp_usd=5e6, volume_usd_per_min=1e6, ttf_ceiling_min=30, per_pair_notional_max=0.35,
                per_instrument_risk_share_max=0.40, open_risk_cap=0.02, sigma_star_annual=0.10)
    base.update(kw)
    return SizingInputs(**base)


@pytest.mark.invariant("INV-03")
def test_t_above_entry_without_breakout_is_rejected():
    ladder, binding = evaluate_entry(ctx(B=0.0, T=0.6))
    assert binding == "NO_BREAKOUT"
    assert evaluate_entry(ctx(side=-1, B=0.0, T=-0.7))[1] == "NO_BREAKOUT"
    assert evaluate_entry(ctx())[1] is None


@pytest.mark.invariant("INV-04")
def test_stop_widen_rejected_and_trail_only_ratchets():
    st = open_stop(100.0, 3.0, 2.0, 3.0)
    assert st.initial == 94.0
    with pytest.raises(StopWidenAttempt):
        st.request(90.0)
    prev = st.active
    for c in [101, 105, 99, 97, 110, 104, 90]:
        cur = st.on_close(c, 3.0, 3.0)
        assert cur >= prev
        prev = cur
    short = open_stop(100.0, 3.0, 2.0, 3.0, side=-1)
    with pytest.raises(StopWidenAttempt):
        short.request(120.0)


@pytest.mark.invariant("INV-04")
def test_replay_stop_lines_monotone():
    r = replay(universe(), POL, StrategyRouter(POL), ReplayConfig(mu_q_daily=0.001))
    assert r.trades
    for t in r.trades:
        seg = [v for ts, v in r.stops[t.instrument_id] if t.entry_time < ts <= t.exit_time]
        assert all(b >= a - 1e-9 for a, b in zip(seg, seg[1:], strict=False)), t
    for t in r.trades:
        assert t.exit_reason in {"TRAILING_STOP", "INITIAL_STOP", "TIME_STOP", "T_CROSSED_ZERO", "RISK_KERNEL", "END_OF_REPLAY"}


@pytest.mark.invariant("INV-05")
def test_v_never_exceeds_one_and_gross_capped():
    low_vol = compute_size(sizing(sigma_daily=0.001))  # book vol << sigma* -> v = 1, not > 1
    assert low_vol.v == 1.0
    hi = compute_size(sizing(sigma_daily=0.10, d=0.2))
    assert 0 < hi.v <= 1.0
    full = compute_size(sizing(gross_now=30_000))
    assert full.size_usd == 0 and full.binding_limit == "spot_gross"


@pytest.mark.invariant("INV-06")
def test_stale_cost_input_rejected_and_no_bypass():
    stale = CostInput(0.006, NOW - timedelta(days=30), timedelta(days=7))
    fresh = CostInput(0.001, NOW, timedelta(minutes=1))
    r = cost_gate(k_stop=2.0, atr_daily=3.0, entry_px=100.0, fee_roundtrip=stale, spread_at_size=fresh,
                  slippage_q75=fresh, cost_R_max=0.15, now=NOW)
    assert not r.passed and r.gate == "COST_INPUT_STALE"
    assert evaluate_entry(ctx(cost_gate="COST_INPUT_STALE"))[1] == "COST_INPUT_STALE"
    params = set(inspect.signature(costmod.cost_gate).parameters)
    assert not {p for p in params if "bypass" in p or "skip" in p or "force" in p}


@pytest.mark.invariant("INV-07")
def test_exits_never_gated_by_evidence():
    params = set(inspect.signature(evaluate_exit).parameters)
    assert not params & {"evidence_on_file", "cost_gate", "risk_budget_ok", "data_certified"}
    assert evaluate_exit(side=1, T=-0.1, stop_hit=False, initial_hit=False, time_stop_state="OK", risk_instruction=False).exit
    # with evidence withdrawn mid-replay the engine still exits (entries stop, exits continue)
    u = universe()
    with_ev = replay(u, POL, StrategyRouter(POL), ReplayConfig(mu_q_daily=0.001))
    no_ev = replay(u, POL, StrategyRouter(POL), ReplayConfig(mu_q_daily=0.001, evidence_on_file=False))
    assert with_ev.trades and not no_ev.trades
    assert no_ev.binding_counts.get("EVIDENCE_NOT_ON_FILE", 0) > 0


@pytest.mark.invariant("INV-08")
def test_add_on_loser_rejected():
    with pytest.raises(AddRejected, match="ADD_ON_LOSER"):
        check_add(open_R=-0.5, adds_so_far=0, gates_pass=True, new_combined_risk_usd=100, r_tier=0.0075, nav=30_000)
    with pytest.raises(AddRejected, match="ADD_BELOW_1R"):
        check_add(open_R=0.8, adds_so_far=0, gates_pass=True, new_combined_risk_usd=100, r_tier=0.0075, nav=30_000)
    with pytest.raises(AddRejected, match="ADD_LIMIT"):
        check_add(open_R=1.5, adds_so_far=1, gates_pass=True, new_combined_risk_usd=100, r_tier=0.0075, nav=30_000)
    with pytest.raises(AddRejected, match="ADD_RISK_EXCEEDS_R"):
        check_add(open_R=1.5, adds_so_far=0, gates_pass=True, new_combined_risk_usd=300, r_tier=0.0075, nav=30_000)
    check_add(open_R=1.2, adds_so_far=0, gates_pass=True, new_combined_risk_usd=200, r_tier=0.0075, nav=30_000)


@pytest.mark.invariant("INV-09")
def test_intraday_trigger_creates_no_decision():
    assert decision_cycle_guard(NOW + timedelta(seconds=90)) == NOW
    for ts in [NOW + timedelta(minutes=37), NOW + timedelta(hours=2), NOW - timedelta(minutes=1)]:
        with pytest.raises(IntradayTriggerIgnored):
            decision_cycle_guard(ts)


def regime_claim(m=0.5, counts=True):
    pn = {k: {"mean": 0.25, "lo90": 0.2, "hi90": 0.3} for k in "UDRS"}
    payload = {"instrument_id": "BTC", "bar_close": "2026-09-28T16:00:00Z", "state": "U", "P_next": pn,
               "ESS": [40, 40, 40, 40], "m_regime": m, "authority": "T0", "binding_reasons": []}
    if counts:
        payload["counts_decayed"] = [[10, 1, 1, 1]] * 4
    return {"claim_id": "r1", "kind": "regime_claim", "class": "ESTIMATED", "snapshot_hash": "a" * 64, "policy_hash": "a" * 64,
            "code_version": "0.1.0", "created_at": "2026-09-28T16:01:00Z", "certified": True, "payload": payload}


@pytest.mark.invariant("INV-10")
def test_regime_cannot_exceed_one_or_touch_size_at_T0():
    validate("regime_claim", regime_claim())
    with pytest.raises(SchemaError):
        validate("regime_claim", regime_claim(m=1.2))
    with pytest.raises(SchemaError):
        validate("regime_claim", regime_claim(counts=False))
    base = compute_size(sizing())
    t0_stress = compute_size(sizing(regime_authority="T0", regime_stress=True, m_regime=0.2))
    assert t0_stress.v == base.v and t0_stress.m_applied == 1.0 and t0_stress.size_usd == base.size_usd
    t1_stress = compute_size(sizing(regime_authority="T1", regime_stress=True, m_regime=0.2))
    assert t1_stress.size_usd < base.size_usd


def evidence_full(nav=35_000):
    today = date(2026, 9, 28)
    return TierEvidence(nav_history=[(today - timedelta(days=i), nav, True) for i in range(40)], shadow_days=95,
                        shadow_recon=0.9995, validation_A_steps_pass=frozenset({1, 2, 3, 4, 5, 7, 8}),
                        prev_tier_live_days=61, canary_cost_divergence=0.1), today


@pytest.mark.invariant("INV-11")
def test_nav_crossing_floor_notifies_but_never_upgrades():
    ev, today = evidence_full()
    router = StrategyRouter(POL, RouterState(user_selected="T1"))
    el = eligibility(POL, ev, today)
    assert el.eligible_tier == "T2"
    router.update_eligibility(el, NOW)
    assert router.state.active == "T1" and router.state.notifications == ["T2 eligible: approve?"]


@pytest.mark.invariant("INV-12")
def test_upgrade_with_failing_gate_or_unsigned_refused():
    ev, today = evidence_full(nav=10_000)  # below T2's $30k floor
    router = StrategyRouter(POL, RouterState(user_selected="T1"))
    el = eligibility(POL, ev, today)
    with pytest.raises(TierRequestRefused) as e:
        router.request_up("T2", el, None, NOW)
    assert e.value.binding_gate == "T2:G1"
    ev2, _ = evidence_full()
    el2 = eligibility(POL, ev2, today)
    router.update_eligibility(el2, NOW)
    with pytest.raises(TierRequestRefused, match="UNSIGNED"):
        router.request_up("T2", el2, {"action": "TIER_UPGRADE"}, NOW)
    a = Authenticator()
    reg = ap.SignerRegistry.from_doc({"signers": [{"signer_id": "principal", "keys": [{"key_id": "k", "public_key": a.public_line}]}]})
    stmt = ap.statement("TIER_UPGRADE", "b" * 64, "principal", "Move to Spot Core after canary", NOW.isoformat())
    va = ap.verify_approval(dict(stmt, signature=a.sign(ap.statement_bytes(stmt), ap.NAMESPACE)), reg,
                            expected_action="TIER_UPGRADE", expected_subject="b" * 64)
    assert router.request_up("T2", el2, va, NOW) == "T2"


@pytest.mark.invariant("INV-41")
def test_deposit_restarts_tier_dwell():
    ev, today = evidence_full()
    ev.deposits = [(today - timedelta(days=3), 20_000, 35_000)]  # crossed the $30k T2 floor, not T1's $5k
    assert eligibility(POL, ev, today).eligible_tier == "T1"


@pytest.mark.invariant("INV-13")
def test_downgraded_sleeve_reduce_only_without_forced_exit():
    ev, today = evidence_full()
    router = StrategyRouter(POL, RouterState(user_selected="T1"))
    router.update_eligibility(eligibility(POL, ev, today), NOW)
    assert router.sleeve_allowed("A_long")
    router.auto_downgrade("DD_12", NOW)
    assert router.state.active == "T0" and not router.sleeve_allowed("A_long", "LIVE")
    assert evaluate_entry(ctx(sleeve_allowed=False))[1] == "TIER_NOT_ACTIVE"
    # the exit rule has no tier input: an open position keeps its stop and exits only on its own rules
    assert "sleeve_allowed" not in inspect.signature(evaluate_exit).parameters
    assert not evaluate_exit(side=1, T=0.4, stop_hit=False, initial_hit=False, time_stop_state="OK", risk_instruction=False).exit


def test_cluster_and_es_limits_bind():
    two = compute_size(sizing(book=[Holding(4_000, 0.03)]))
    one = compute_size(sizing())
    assert two.size_ES < one.size_ES
