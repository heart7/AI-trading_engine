#!/usr/bin/env python3
"""End-of-day and monthly reports (spec §12.7) for the paper session. FIXTURE until real data is certified.

  python3 tools/report.py eod --out runs/reports
  python3 tools/report.py monthly --out runs/reports
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.bff.session import PaperSession  # noqa: E402
from engine.reports.reports import build, to_markdown  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("kind", choices=["eod", "monthly"])
    p.add_argument("--out", default="runs/reports")
    a = p.parse_args()
    rep = build(PaperSession.build(), a.kind)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    stem = f"{a.kind}-{rep['on']}"
    (out / f"{stem}.md").write_text(to_markdown(rep))
    (out / f"{stem}.json").write_text(json.dumps(rep, indent=2, default=str) + "\n")
    print(f"wrote {out / stem}.md ({len(rep['references'])} referenced claims{', FIXTURE' if rep['fixture'] else ''})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
