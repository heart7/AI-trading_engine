"""P8 learning loop: episode factory (L1), episode store, attribution on episodes (L2), cost-model retune proposals."""
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from engine.bff.session import FIXTURE_MU_Q, fixture_universe
from engine.common.canonical import content_hash
from engine.common.schemas import SchemaError
from engine.data.store import ChainBroken
from engine.evidence.attribution import attribute
from engine.policy.loader import load_policy, thaw
from engine.replay.paper import CostModel, ReplayConfig, replay
from engine.router.router import StrategyRouter
from research.learner import cost_retune, episodes
from research.learner.episodes import EpisodeStore, Side, build
from research.registry import registry

ROOT = Path(__file__).resolve().parents[2]
POL = load_policy()
DOC = thaw(POL.doc)


@pytest.fixture(scope="module")
def paper():
    series = fixture_universe(DOC, 3000)
    res = replay(series, DOC, StrategyRouter(DOC), ReplayConfig(mu_q_daily=FIXTURE_MU_Q), CostModel())
    return series, res, episodes.from_replay(res, series, DOC, POL.hash)


def ep(cls="OBSERVED", entry_fill=100.1, exit_fill=109.9, reason="SIGNAL_EXIT", flags=(), liq="taker", at="2026-10-01T00:00:00+00:00"):
    return build(instrument_id="XBTUSD", sleeve="A_long", qty=1.0, entry=Side(100.0, 100.0, 100.1, entry_fill, liq),
                 exit=Side(110.0, 110.0, 109.89, exit_fill, "taker"), entry_bar_close=at,
                 exit_bar_close="2026-10-05T00:00:00+00:00", fees_usd=0.5, risk_usd=5.0, hold_bars=24, exit_reason=reason,
                 cls=cls, source="FILLS" if cls == "OBSERVED" else "PAPER_REPLAY", policy_hash=POL.hash,
                 expected_hold=60, flags=flags)


def quotes(values, cls="OBSERVED", pred=0.001):
    return [{"bar_close": f"2026-10-{1 + i // 6:02d}T{4 * (i % 6):02d}:00:00+00:00", "class": cls,
             "instruments": [{"instrument_id": "XBTUSD", "cost_predicted": pred, "cost_observed": v}]}
            for i, v in enumerate(values)]


# ---------- episode factory ----------
def test_replay_episodes_one_per_closed_trade_and_fixture(paper):
    _, res, eps = paper
    done = episodes.matured(res)
    assert len(eps) == len(done) > 0 and all(t.exit_reason != "END_OF_REPLAY" for t in done)
    for e, t in zip(eps, done):
        assert e["class"] == "FIXTURE" and e["source"] == "PAPER_REPLAY"
        assert e["pnl_usd"] == pytest.approx(t.pnl, abs=1e-9) and e["R_realised"] == pytest.approx(t.R, abs=1e-9)
        assert e["hold_bars"] == t.bars_held and e["exit_reason"] == t.exit_reason
        assert e["expected_hold"] == DOC["stops"]["time_stop"]["default_dwell_bars"]
        for side in ("entry", "exit"):  # paper fills equal the model by construction
            assert e[side]["cost_realised"] == pytest.approx(e[side]["cost_predicted"], abs=1e-12)
            assert e[side]["model_px"] == e[side]["fill_px"]


def test_replay_side_costs_match_cost_model(paper):
    c = CostModel()
    for e in paper[2]:
        if e["entry"]["liquidity"] == "taker":
            assert e["entry"]["cost_predicted"] == pytest.approx(c.half_spread + c.slippage_q75)
        want = c.slippage_q75 if e["exit_reason"] in episodes.STOP_REASONS else c.half_spread + c.slippage_q75
        assert e["exit"]["cost_predicted"] == pytest.approx(want)


def test_attribution_on_episodes_is_exact_and_matches_replay(paper):
    _, res, eps = paper
    att = attribute([episodes.to_attribution(e) for e in eps])
    assert att.exact
    assert float(att.net) == pytest.approx(sum(t.pnl for t in episodes.matured(res)), abs=1e-6)
    assert episodes.process_error_rate(eps) == 0.0  # paper fills equal the model: no COST_MODEL_OFF


def test_excluded_classes_kept_but_never_trained():
    for cls in episodes.EXCLUDED_CLASSES:
        e = ep(reason=cls)
        assert e["classification"] == cls and e["excluded_from_training"]
    assert not ep()["excluded_from_training"]


def test_cost_model_off_is_a_process_error():
    ok, bad = ep(), ep(entry_fill=100.5)  # 50 bp paid against 10 bp predicted
    assert ok["classification"] == "OUTCOME_VARIANCE" and "COST_MODEL_OFF" not in ok["process_flags"]
    assert bad["classification"] == "PROCESS_ERROR" and bad["process_error_class"] == "execution"
    assert bad["process_flags"] == ["COST_MODEL_OFF"]
    assert episodes.process_error_rate([ok, bad, ep(reason="FORCED_FLATTEN")]) == 0.5
    assert episodes.process_error_rate([]) is None


def test_given_flags_classify_and_dedupe():
    e = ep(flags=("STALE_INPUT", "STALE_INPUT"))
    assert e["process_flags"] == ["STALE_INPUT"] and e["process_error_class"] == "data"


def test_episode_schema_rejects_bad_records():
    e = ep()
    e["class"] = "HYPOTHESIS"
    with pytest.raises(SchemaError):
        episodes.validate("trade_episode", e)


def test_realised_costs_measure_against_arrival():
    e = ep(entry_fill=100.3, exit_fill=109.78)
    assert e["entry"]["cost_realised"] == pytest.approx(0.003)
    assert e["exit"]["cost_realised"] == pytest.approx(0.002)
    assert e["costs"]["realised_usd"] == pytest.approx(0.3 + 0.22)
    assert e["costs"]["predicted_usd"] == pytest.approx(0.1 + 0.11)


# ---------- store ----------
def test_store_is_idempotent_chained_and_tamper_evident(tmp_path, paper):
    s = EpisodeStore(tmp_path / "ep.jsonl")
    n = s.extend(paper[2])
    assert n == len(paper[2]) and s.extend(paper[2]) == 0
    assert EpisodeStore(tmp_path / "ep.jsonl").extend(paper[2]) == 0  # ids survive a reload
    assert s.verify() == n
    lines = (tmp_path / "ep.jsonl").read_text().splitlines()
    lines[0] = lines[0].replace('"FIXTURE"', '"OBSERVED"', 1)
    (tmp_path / "ep.jsonl").write_text("\n".join(lines) + "\n")
    with pytest.raises(ChainBroken):
        EpisodeStore(tmp_path / "ep.jsonl").verify()


def test_training_set_is_observed_and_not_excluded(tmp_path, paper):
    s = EpisodeStore(tmp_path / "ep.jsonl")
    s.extend(paper[2])
    assert s.training_set() == []
    s.add(ep())
    s.add(ep(reason="PROTECTION_FAILURE", at="2026-10-02T00:00:00+00:00"))
    assert [e["exit_reason"] for e in s.training_set()] == ["SIGNAL_EXIT"]
    assert EpisodeStore(tmp_path / "missing.jsonl").records() == []


# ---------- cost retune ----------
def test_observations_skip_maker_excluded_and_unpriced():
    eps = [ep(liq="maker"), ep(reason="ADL_EVENT", at="2026-10-02T00:00:00+00:00")]
    j = quotes([0.002]) + [{"bar_close": "2026-10-09T00:00:00+00:00", "class": "OBSERVED",
                            "instruments": [{"instrument_id": "X", "cost_predicted": 0.001, "cost_observed": None}, {"instrument_id": "Y"}]}]
    obs = cost_retune.observations(eps, j)
    assert [o["source"] for o in obs] == ["FILL", "SHADOW_QUOTE"]  # the maker episode's taker exit, and one quote


def test_fixture_is_never_fitted(paper):
    p = cost_retune.fit(cost_retune.observations(paper[2]))
    assert p["verdict"] == "NO_EVIDENCE" and p["evidence_class"] == "FIXTURE" and not p["applies"]
    p = cost_retune.fit(cost_retune.observations(journal=quotes([0.01] * 500, cls="FIXTURE")))
    assert p["verdict"] == "NO_EVIDENCE" and "proposed" not in p
    assert cost_retune.fit([])["evidence_class"] == "NONE"


def test_insufficient_evidence():
    p = cost_retune.fit(cost_retune.observations(journal=quotes([0.004] * 20)))
    assert p["verdict"] == "INSUFFICIENT" and "proposed" not in p and p["fitted"] == pytest.approx(0.004)


def test_raise_on_quotes_tightens_model():
    vals = [0.0015 + 0.0001 * (i % 10) for i in range(150)]
    p = cost_retune.fit(cost_retune.observations(journal=quotes(vals)))
    assert p["verdict"] == "RAISE" and not p["loosens"] and not p["applies"]
    assert p["interval"][0] > p["current"]["side_charge"]
    assert p["proposed"]["slippage_q75"] == pytest.approx(p["fitted"] - CostModel().half_spread, abs=1e-6)
    assert p["cost_divergence"] == pytest.approx(abs(sum(vals) - 0.001 * 150) / (0.001 * 150))
    assert p == cost_retune.fit(cost_retune.observations(journal=quotes(vals)))  # seeded: reproducible


def test_quotes_alone_can_never_loosen():
    p = cost_retune.fit(cost_retune.observations(journal=quotes([0.0003] * 300)))
    assert p["verdict"] == "NO_CHANGE" and "real fills" in p["detail"] and "proposed" not in p


def test_lower_needs_real_fills_and_flags_loosening():
    eps = [ep(entry_fill=100.02, exit_fill=109.98, at=f"2026-10-{1 + i // 6:02d}T{4 * (i % 6):02d}:00:00+00:00")
           for i in range(60)]
    p = cost_retune.fit(cost_retune.observations(eps))
    assert p["n"]["fills"] == 120 and p["verdict"] == "LOWER" and p["loosens"]
    assert any("step 4" in r for r in p["requires"]) and not p["applies"]
    assert p["proposed"]["slippage_q75"] < CostModel().slippage_q75


def test_no_change_inside_interval():
    import numpy as np
    vals = list(np.random.default_rng(0).normal(0.001 - 0.6745 * 0.0003, 0.0003, 200))  # true q75 = the 0.001 charge
    p = cost_retune.fit(cost_retune.observations(journal=quotes(vals)))
    lo, hi = p["interval"]
    assert p["verdict"] == "NO_CHANGE" and lo <= p["current"]["side_charge"] <= hi


def test_effect_compares_on_copies_and_never_applies():
    series = fixture_universe(DOC, 1500)
    before = content_hash(DOC)
    p = cost_retune.fit(cost_retune.observations(journal=quotes([0.004 + 0.0001 * (i % 5) for i in range(150)])))
    eff = cost_retune.effect(DOC, series, p, mu_q_daily=FIXTURE_MU_Q)
    assert eff["compared"] and not eff["applies"] and content_hash(DOC) == before
    assert eff["proposed"]["trades"] <= eff["current"]["trades"]  # a stricter cost model never adds trades
    assert cost_retune.effect(DOC, series, {"verdict": "NO_CHANGE"}) == {"compared": False, "reason": "NO_CHANGE"}


def test_retune_has_no_write_path_and_never_targets_budgets():
    for mod in ("research/learner/cost_retune.py", "research/learner/episodes.py"):
        src = (ROOT / mod).read_text()
        assert not re.search(r"write_text|open\([^)]*['\"]w|policy/|approvals", src), mod
    assert cost_retune.TARGET not in cost_retune.FORBIDDEN_TARGETS
    assert "cost_R_max" not in cost_retune.TARGET and "N_max" not in cost_retune.TARGET


def test_cost_hypothesis_is_registered():
    items = registry.load()
    h = next(h for h in items if h["id"] == cost_retune.HYPOTHESIS_ID)
    assert h["status"] == "PRE_REGISTERED" and h["harness_step"] == 4
    assert registry.budget_used(items, 2026) <= DOC["governance"]["hypothesis_budget_per_year"]


# ---------- CLI ----------
def test_learning_drill_cli(tmp_path):
    r = subprocess.run([sys.executable, "tools/learn.py", "drill", "--out", str(tmp_path), "--bars", "2000"], cwd=ROOT,
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    rec = json.loads((tmp_path / "learning_fixture.json").read_text())
    assert rec["passed"] and rec["class"] == "FIXTURE" and rec["training_set"] == 0 and not rec["retune"]["applies"]


def test_retune_cli_on_observed_quotes(tmp_path):
    from research.learner.episodes import AppendOnlyLog
    j = AppendOnlyLog(tmp_path / "journal.jsonl")
    for rec in quotes([0.003] * 120):
        j.append(rec)
    r = subprocess.run([sys.executable, "tools/learn.py", "retune", "--journal", str(tmp_path / "journal.jsonl"),
                        "--episodes", str(tmp_path / "none.jsonl"), "--out", str(tmp_path)], cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    p = json.loads((tmp_path / "cost-proposal.json").read_text())
    assert p["verdict"] == "RAISE" and p["applies"] is False and p["policy_hash"] == POL.hash
