from engine.replay.paper import FUNNEL, ReplayConfig, replay
from engine.router.router import StrategyRouter
from tests.helpers.fixtures import policy, universe

POL = policy()


def test_replay_is_bit_for_bit_reproducible():
    u = universe()
    a = replay(u, POL, StrategyRouter(POL), ReplayConfig(mu_q_daily=0.001))
    b = replay(u, POL, StrategyRouter(POL), ReplayConfig(mu_q_daily=0.001))
    assert a.result_hash == b.result_hash and a.trades and len(a.trades) == len(b.trades)


def test_funnel_non_increasing_and_sums():
    r = replay(universe(), POL, StrategyRouter(POL), ReplayConfig(mu_q_daily=0.001))
    counts = [r.funnel[k] for k in FUNNEL]
    assert all(x >= y for x, y in zip(counts, counts[1:], strict=False))
    assert sum(r.binding_counts.values()) == r.funnel["evaluated"]


def test_no_mu_q_means_no_trades():
    r = replay(universe(), POL, StrategyRouter(POL), ReplayConfig(mu_q_daily=None))
    assert not r.trades and r.summary()["class"] == "REPORTED"


def test_fixture_results_are_reported_class_and_assumptions_listed():
    r = replay(universe(), POL, StrategyRouter(POL), ReplayConfig(mu_q_daily=0.001))
    assert r.summary()["class"] == "REPORTED"
    assert "sigma_star=anchor (step 8 not run)" in r.assumed


def test_intents_persist_every_outcome_with_full_ladder():
    r = replay(universe(), POL, StrategyRouter(POL), ReplayConfig(mu_q_daily=0.001))
    assert r.intents_tail
    for it in r.intents_tail:
        assert len(it["gate_ladder"]) == 17
        first_fail = next((g["gate"] for g in it["gate_ladder"] if not g["passed"]), None)
        assert first_fail == it["binding_gate"]
