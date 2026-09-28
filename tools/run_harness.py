#!/usr/bin/env python3
"""Run validation harness steps 1-5, 7, 8 (spec §9.3) and write run records + verdicts.

  python3 tools/run_harness.py --fixture --years 9 --out runs/harness-fixture
  python3 tools/run_harness.py --fixture --es-mult 3.0 --out runs/harness-es3   # ES-limit study
  python3 tools/run_harness.py --fixture --sleeve B_short --out runs/harness-b-short  # Strategy B book (PAPER)

FIXTURE inputs produce REPORTED-class records: they exercise the harness, they are not evidence.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.common.canonical import content_hash  # noqa: E402
from engine.data.bars import H4, to_arrays  # noqa: E402
from engine.data.certify import certify  # noqa: E402
from engine.data.fixtures import fixture_bars, second_source  # noqa: E402
from engine.policy.loader import load_policy  # noqa: E402
from engine.replay.paper import Series  # noqa: E402
from research.harness import steps  # noqa: E402
from research.harness.verdicts import allowed_mode  # noqa: E402


def fixture_universe(years: float):
    start = datetime(2016, 1, 1, tzinfo=timezone.utc)
    n = int(365 * years) * 6
    series, reports = [], {}
    for j, name in enumerate(["FIXTURE_BTC", "FIXTURE_ETH", "FIXTURE_XRP", "FIXTURE_SOL"]):
        bars = fixture_bars(name, start, n, seed=100 + j, vol_daily=0.03 + 0.01 * j)
        ds = certify(name, {"fixture-a": bars, "fixture-b": second_source(bars, seed=200 + j)}, start=start,
                     end=start + n * H4, fixture=True)
        reports[name] = ds.report.as_dict()
        a = to_arrays(bars)
        series.append(Series(name, a["open_time"], a["o"], a["h"], a["l"], a["c"]))
    return series, reports


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixture", action="store_true", required=True)
    ap.add_argument("--years", type=float, default=9.0)
    ap.add_argument("--reps", type=int, default=1000)
    ap.add_argument("--es-mult", type=float)
    ap.add_argument("--sleeve", default="A_long", choices=["A_long", "B_short", "B_long"])
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    pol = load_policy()
    doc = pol.doc
    if a.es_mult is not None:  # research variant: a hypothesis-registry debit, never a live change
        doc = json.loads(json.dumps(doc))
        doc["risk"]["es975_mult"] = a.es_mult
    series, reports = fixture_universe(a.years)
    runner = None
    if a.sleeve != "A_long":
        from tests.helpers.fixtures import contract_specs, fixture_funding
        specs = contract_specs()
        runner = steps.perp_runner(series, doc, specs, fixture_funding(series, specs), a.sleeve)
    ctx = steps.HarnessContext(series=series, policy=doc, policy_hash=content_hash(doc), quality_reports=reports, reps=a.reps,
                               n_trials=len(__import__("research.registry.registry", fromlist=["load"]).load()),
                               sleeve=a.sleeve, runner=runner)
    recs = steps.run_all(ctx)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    verdicts = [{"step": r.step, "verdict": r.verdict, "run_id": r.run_id, "metric": r.metric, "value": r.value,
                 "ci": list(r.ci), "details": r.details} for r in recs]
    summary = steps.base(ctx).summary() if recs[0].verdict == "PASS" else {}
    doc_out = {"class": "REPORTED (FIXTURE)", "sleeve": a.sleeve, "policy_hash": content_hash(doc), "base_policy_hash": pol.hash, "es975_mult": doc["risk"]["es975_mult"],
               "allowed_mode": allowed_mode(recs), "base_replay": summary, "funnel": steps.base(ctx).funnel,
               "sizing_binding": steps.base(ctx).sizing_binding, "verdicts": verdicts, "runs": ctx.runs}
    (out / "harness.json").write_text(json.dumps(doc_out, indent=2, default=str) + "\n")
    for r in recs:
        print(f"step {r.step}: {r.verdict:<8} {r.metric} = {r.value} ci={r.ci} run={r.run_id}")
    print("allowed mode:", doc_out["allowed_mode"], "| base:", summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
