import sys
from pathlib import Path

from engine.policy.loader import load_policy
from research.harness import steps

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
from run_harness import fixture_universe  # noqa: E402


def test_harness_end_to_end_on_fixture():
    pol = load_policy()
    series, reports = fixture_universe(3.5)
    ctx = steps.HarnessContext(series=series, policy=pol.doc, policy_hash=pol.hash, quality_reports=reports, reps=60)
    recs = steps.run_all(ctx)
    assert [r.step for r in recs] == [1, 2, 3, 4, 5, 7, 8]
    assert all(r.run_id and r.run_id.startswith(f"bt-s{r.step}-") for r in recs)
    assert all(run["class"] == "REPORTED" for run in ctx.runs)  # fixture evidence is never DERIVED
    s2 = recs[1]
    assert s2.verdict in ("PASS", "FAIL") and (s2.verdict == "FAIL" or s2.ci[0] > 0)


def test_step1_fail_blocks_later_steps():
    pol = load_policy()
    series, reports = fixture_universe(1.2)
    reports.pop("FIXTURE_SOL")
    ctx = steps.HarnessContext(series=series, policy=pol.doc, policy_hash=pol.hash, quality_reports=reports, reps=20)
    recs = steps.run_all(ctx)
    assert recs[0].verdict == "FAIL" and all(r.verdict == "NOT_RUN" for r in recs[1:])
