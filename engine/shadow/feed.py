"""Live, keyless inputs for the SHADOW runner (spec §8.1, §11.1, §19 P6).

Everything here reads public market-data endpoints only: no API key, no account, no order path. It is meant to run
on the principal's own machine (the build container cannot reach exchange APIs, so live fetches are implemented and
unverified; parsers are tested on recorded payload shapes).

- `load_history` reads the certified 4h store written by tools/build_dataset.py and returns the longest contiguous
  certified run ending at the newest bar, as a replay Series.
- `refresh` fetches the latest bars from Kraken (primary), Binance and Bybit (re-quoted from USDT through Kraken
  USDT/USD, never assumed at par), certifies them from >= 2 sources and appends bars newer than the store's head.
- `kraken_quote` turns Kraken's public order book into the Quote the runner prices would-be entries with.
"""
from __future__ import annotations

import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np

from engine.data.bars import H4, Bar
from engine.data.certify import certify
from engine.data.sources import public_ohlc as src
from engine.data.store import AppendOnlyLog
from engine.replay.paper import Series
from engine.shadow.runner import Quote

KRAKEN_PAIRS = {"BTC": "XBTUSD", "ETH": "ETHUSD", "XRP": "XRPUSD", "SOL": "SOLUSD"}
KRAKEN_DEPTH = "https://api.kraken.com/0/public/Depth"
REFRESH_BARS = 30  # five days of 4h bars per refresh; the store holds the rest


def store_path(history_dir: Path, base: str) -> Path:
    return Path(history_dir) / f"{base}-USD.bars.jsonl"


def load_history(path: Path) -> Series | None:
    if not Path(path).exists():
        return None
    recs = [r for r in AppendOnlyLog(Path(path)).records() if r.get("certified")]
    if not recs:
        return None
    by_t: dict[int, dict[str, Any]] = {}
    for r in recs:
        by_t[int(datetime.fromisoformat(r["open_time"]).timestamp())] = r
    ts = sorted(by_t)
    step = int(H4.total_seconds())
    start = len(ts) - 1
    while start > 0 and ts[start] - ts[start - 1] == step:
        start -= 1
    run = [by_t[t] for t in ts[start:]]
    arr = lambda k: np.array([float(r[k]) for r in run])  # noqa: E731
    return Series(run[-1]["instrument_id"], np.array(ts[start:], dtype=np.int64), arr("o"), arr("h"), arr("l"), arr("c"),
                  certified=True)


def refresh(history_dir: Path, base: str, now: datetime, fetch: src.Fetch = src._http_json) -> int:
    """Append newly certified bars for `base`/USD. Returns how many bars were added."""
    path = store_path(history_dir, base)
    log = AppendOnlyLog(path)
    have = {r["open_time"] for r in log.records()} if path.exists() else set()
    end = now - (now - datetime(1970, 1, 1, tzinfo=timezone.utc)) % H4
    since = end - REFRESH_BARS * H4
    kr = src.fetch_kraken(KRAKEN_PAIRS[base], since, now, fetch=fetch)
    usdt = {b.open_time: b.c for b in src.fetch_kraken("USDTUSD", since, now, fetch=fetch)}
    bn = src.convert_quote(src.fetch_binance(f"{base}USDT", since, now, fetch=fetch), usdt)
    by = src.convert_quote(src.fetch_bybit(f"{base}USDT", since, now, fetch=fetch), usdt)
    ds = certify(f"kraken-spot:{base}/USD", {"kraken-spot": kr, "binance-spot": bn, "bybit-v5-spot": by},
                 start=since, end=end)
    added = 0
    for cb in ds.bars:
        b: Bar = cb.bar
        if b.open_time.isoformat() in have:
            continue
        log.append({"instrument_id": cb.instrument_id, "open_time": b.open_time.isoformat(), "o": b.o, "h": b.h,
                    "l": b.l, "c": b.c, "v": b.v, "sources": list(cb.sources), "era": cb.era,
                    "dispersion": cb.dispersion, "certified": cb.certified, "content_hash": cb.content_hash})
        added += 1
    return added


def parse_kraken_depth(payload: dict, now: datetime, band: float = 0.005) -> Quote:
    if payload.get("error"):
        raise RuntimeError(f"kraken error: {payload['error']}")
    book = next(iter(payload["result"].values()))
    asks = sorted((float(p), float(v)) for p, v, *_ in book["asks"])
    bids = sorted(((float(p), float(v)) for p, v, *_ in book["bids"]), reverse=True)
    if not asks or not bids:
        raise RuntimeError("empty order book")
    bid, ask = bids[0][0], asks[0][0]
    mid = (bid + ask) / 2
    depth = sum(p * v for p, v in asks if p <= mid * (1 + band))  # a long entry buys through the asks
    return Quote(bid, ask, depth, now)


def kraken_quote(base: str, now: datetime, fetch: src.Fetch = src._http_json) -> Quote:
    q = urllib.parse.urlencode({"pair": KRAKEN_PAIRS[base], "count": 500})
    return parse_kraken_depth(fetch(f"{KRAKEN_DEPTH}?{q}"), now)


def last_close(now: datetime) -> datetime:
    return now - (now - datetime(1970, 1, 1, tzinfo=timezone.utc)) % H4


def is_due(journal_records: list[dict[str, Any]], now: datetime, lag: timedelta = timedelta(minutes=5)) -> bool:
    """A cycle is due once per close, after the certification lag."""
    close = last_close(now)
    if now - close < lag:
        return False
    return not any(r["bar_close"] == close.isoformat() for r in journal_records)
