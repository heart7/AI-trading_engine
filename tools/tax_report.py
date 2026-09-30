#!/usr/bin/env python3
"""UK capital gains matching report (spec §12.4, §12.7). ASSUMED rules; not tax advice.

  python3 tools/tax_report.py --transactions trades.csv --fx gbpusd.csv --out runs/tax

trades.csv: ts,account,asset,side,qty,value_usd,fee_usd,ref   (ts ISO 8601 UTC; side BUY or SELL)
gbpusd.csv: date,gbp_per_usd                                    (certified daily rate)
Writes disposals.csv (one row per match, for the accountant) and summary.json (per tax year).
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.tax.uk import EXPORT_COLUMNS, Transaction, compute, export_rows, load_tax_policy, summary  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--transactions", required=True)
    p.add_argument("--fx", required=True)
    p.add_argument("--out", default="runs/tax")
    a = p.parse_args()
    with open(a.transactions, newline="") as f:
        tx = [Transaction(datetime.fromisoformat(r["ts"]), r["account"], r["asset"], r["side"].upper(), Decimal(r["qty"]),
                          Decimal(r["value_usd"]), Decimal(r.get("fee_usd") or 0), r.get("ref", "")) for r in csv.DictReader(f)]
    with open(a.fx, newline="") as f:
        fx = {date.fromisoformat(r["date"]): Decimal(r["gbp_per_usd"]) for r in csv.DictReader(f)}
    disposals, pools = compute(tx, fx)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    with (out / "disposals.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=EXPORT_COLUMNS)
        w.writeheader()
        w.writerows(export_rows(disposals))
    s = summary(disposals, load_tax_policy())
    (out / "summary.json").write_text(json.dumps({"years": s, "pools": pools, "note": "ASSUMED rules; not tax advice"},
                                                 indent=2, default=str) + "\n")
    for ty, y in s.items():
        print(f"{ty}: {y['disposals']} disposals, net £{y['net_gbp']}, reserve {y['reserve_gbp'] or y['reserve_status']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
