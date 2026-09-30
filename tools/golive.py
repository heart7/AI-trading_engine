#!/usr/bin/env python3
"""Go-live readiness checklist (spec §19 P7). Reads files only; activates nothing.

  python3 tools/golive.py [--policy policy/policy-10.4.0.yaml] [--journal runs/shadow/journal.jsonl]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.governance.golive import checklist, ready, today_utc  # noqa: E402
from engine.policy.loader import load_policy  # noqa: E402
from engine.shadow.runner import ShadowJournal  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--policy")
    p.add_argument("--journal")
    a = p.parse_args()
    journal = ShadowJournal(Path(a.journal)).records() if a.journal and Path(a.journal).exists() else []
    items = checklist(load_policy(a.policy), today=today_utc(), journal=journal)
    for i in items:
        print(f"  {'MET    ' if i['met'] else 'NOT MET'} {i['item']} {i['label']} -- {i['detail']} [{i['owner']}]")
    ok = ready(items)
    print("READY FOR LIVE" if ok else f"NOT READY: {sum(not i['met'] for i in items)} of {len(items)} items open")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
