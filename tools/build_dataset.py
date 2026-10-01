#!/usr/bin/env python3
"""Build a certified 4h dataset and its quality report (spec §11, phase P1).

  python3 tools/build_dataset.py --fixture --out data/fixture     # offline FIXTURE build
  python3 tools/build_dataset.py --live --pair BTC --out data/live # needs exchange network access

Live mode reads public endpoints only (no keys): Kraken <pair>/USD as the primary, Bitstamp <pair>/USD, and Binance
and Bybit <pair>/USDT re-quoted to USD via Kraken USDT/USD. Venues that refuse the request are skipped and named. Kraken's public OHLC returns only the
latest 720 bars; `--days` beyond 120 backfills from Bitstamp alone (those bars are single-source, so they need
--allow-single-source and are stored flagged). A paid vendor remains the spec's deep-history route (§21 item 11).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.data.bars import H4  # noqa: E402
from engine.data.certify import certify  # noqa: E402
from engine.data.fixtures import fixture_bars, fixture_start, second_source  # noqa: E402
from engine.data.sources import public_ohlc as src  # noqa: E402
from engine.data.store import AppendOnlyLog  # noqa: E402

ERAS = [("era-2014", datetime(2014, 1, 1, tzinfo=timezone.utc)), ("era-2018", datetime(2018, 1, 1, tzinfo=timezone.utc)),
        ("era-2021", datetime(2021, 1, 1, tzinfo=timezone.utc)), ("era-2024", datetime(2024, 1, 1, tzinfo=timezone.utc))]
KRAKEN_PAIRS = {"BTC": "XBTUSD", "ETH": "ETHUSD", "XRP": "XRPUSD", "SOL": "SOLUSD"}


def write(out: Path, name: str, ds) -> None:
    log = AppendOnlyLog(out / f"{name}.bars.jsonl")
    for cb in ds.bars:
        b = cb.bar
        log.append({"instrument_id": cb.instrument_id, "open_time": b.open_time.isoformat(), "o": b.o, "h": b.h,
                    "l": b.l, "c": b.c, "v": b.v, "sources": list(cb.sources), "era": cb.era,
                    "dispersion": cb.dispersion, "certified": cb.certified, "single_source": cb.single_source,
                    "content_hash": cb.content_hash})
    (out / f"{name}.quality.json").write_text(json.dumps(ds.report.as_dict(), indent=2) + "\n")
    r = ds.report
    print(f"{name}: {r.certified_bars}/{r.expected_bars} bars ({r.coverage:.2%}), gaps={len(r.gaps)}, "
          f"quarantined={len(r.quarantined)}, splices={len(r.splices)}, fixture={r.fixture}")


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--fixture", action="store_true")
    g.add_argument("--live", action="store_true")
    ap.add_argument("--pair", action="append")
    ap.add_argument("--years", type=float, default=9.0)
    ap.add_argument("--out", required=True)
    ap.add_argument("--days", type=int, default=120,
                    help="live history depth; Kraken serves only the last 120 days, older bars come from Bitstamp alone")
    ap.add_argument("--allow-single-source", action="store_true",
                    help="certify Kraken-only bars (flagged single_source) when no second venue answers")
    a = ap.parse_args()
    out = Path(a.out)
    now = datetime.now(timezone.utc)
    end = now - (now - datetime(1970, 1, 1, tzinfo=timezone.utc)) % H4
    if a.fixture:
        start = fixture_start(a.years, end)
        n = int((end - start) / H4)
        for i, name in enumerate(a.pair or ["FIXTURE_BTC", "FIXTURE_ETH", "FIXTURE_XRP", "FIXTURE_SOL"]):
            p = fixture_bars(name, start, n, seed=100 + i, vol_daily=0.03 + 0.01 * i)
            ds = certify(name, {"fixture-a": p, "fixture-b": second_source(p, seed=200 + i, drop=0.002)},
                         start=start, end=end, eras=ERAS, fixture=True)
            write(out, name, ds)
        return 0
    for base in a.pair or ["BTC"]:
        since = end - a.days * 6 * H4
        per_source, skipped = src.venue_sources(base, KRAKEN_PAIRS[base], since, now)
        for venue, why in skipped.items():
            print(f"{base}: skipped {venue} ({why})", file=sys.stderr)
        ds = certify(f"kraken-spot:{base}/USD", per_source,
                     start=min(b[0].open_time for b in per_source.values() if b), end=end, eras=ERAS,
                     single_venue=a.allow_single_source)
        write(out, f"{base}-USD", ds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
