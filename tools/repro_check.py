#!/usr/bin/env python3
"""Reproducibility spot-check (spec §13.5): replay a pinned FIXTURE universe twice and compare with the golden hash.

  python3 tools/repro_check.py            # check
  python3 tools/repro_check.py --update   # re-pin after an intended decision-plane change (commit the diff)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.policy.loader import load_policy  # noqa: E402
from engine.replay.paper import ReplayConfig, replay  # noqa: E402
from engine.router.router import StrategyRouter  # noqa: E402
from tests.helpers.fixtures import universe  # noqa: E402

GOLDEN = ROOT / "tests" / "replay" / "golden.json"


def run() -> dict:
    pol = load_policy()
    r = replay(universe(), pol.doc, StrategyRouter(pol.doc), ReplayConfig(mu_q_daily=0.001))
    return {"policy_hash": pol.hash, "result_hash": r.result_hash, "trades": len(r.trades), "funnel": r.funnel}


def main() -> int:
    a, b = run(), run()
    if a != b:
        print("NON-DETERMINISTIC: two runs differ", a, b)
        return 1
    if "--update" in sys.argv:
        GOLDEN.write_text(json.dumps(a, indent=2) + "\n")
        print("pinned", a["result_hash"])
        return 0
    g = json.loads(GOLDEN.read_text())
    if g != a:
        print("REPLAY CHANGED vs golden:\n golden", g, "\n now   ", a)
        return 1
    print("reproducible:", a["result_hash"], f"trades={a['trades']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
