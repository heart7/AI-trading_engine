"""Public market-data sources for 4h OHLCV (spec §8.1: Binance and Bybit are data sources; Kraken executes).

Parsers are pure and tested on recorded payload shapes. Fetchers use only public, unauthenticated
endpoints (no API key). Status: parsers run-verified on sample payloads; live fetches implemented,
unverified (the build container cannot reach exchange APIs). Endpoints ASSUMED per A-PERMISSION-PROBES
review; verify against current venue docs.
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from engine.data.bars import H4, Bar

Fetch = Callable[[str], Any]


ALLOWED_HOSTS = ("https://api.kraken.com/", "https://api.binance.com/", "https://api.bybit.com/")


def _http_json(url: str) -> Any:
    if not url.startswith(ALLOWED_HOSTS):
        raise ValueError(f"host not allowed: {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "uchfe-data/0.1"})  # noqa: S310 - https allowlist above
    with urllib.request.urlopen(req, timeout=20) as r:  # noqa: S310 - fixed https venue hosts
        return json.loads(r.read())


def _utc(sec: float) -> datetime:
    return datetime.fromtimestamp(sec, tz=timezone.utc)


def _complete(bars: list[Bar], now: datetime) -> list[Bar]:
    # A bar is only usable after it has closed; venues return the in-progress bar last.
    return [b for b in bars if b.close_time <= now]


# --- Kraken spot: GET /0/public/OHLC?pair=XBTUSD&interval=240&since=<unix s> ---
KRAKEN = "https://api.kraken.com/0/public/OHLC"


def parse_kraken(payload: dict, now: datetime) -> list[Bar]:
    if payload.get("error"):
        raise RuntimeError(f"kraken error: {payload['error']}")
    res = payload["result"]
    key = next(k for k in res if k != "last")
    bars = [Bar(_utc(int(r[0])), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[6])) for r in res[key]]
    return _complete(sorted(bars, key=lambda b: b.open_time), now)


def fetch_kraken(pair: str, since: datetime, now: datetime, fetch: Fetch = _http_json) -> list[Bar]:
    q = urllib.parse.urlencode({"pair": pair, "interval": 240, "since": int(since.timestamp())})
    return parse_kraken(fetch(f"{KRAKEN}?{q}"), now)


# --- Binance spot: GET /api/v3/klines?symbol=BTCUSDT&interval=4h&startTime=<ms>&limit=1000 ---
BINANCE = "https://api.binance.com/api/v3/klines"


def parse_binance(payload: list, now: datetime) -> list[Bar]:
    bars = [Bar(_utc(r[0] / 1000), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])) for r in payload]
    return _complete(sorted(bars, key=lambda b: b.open_time), now)


def fetch_binance(symbol: str, start: datetime, now: datetime, fetch: Fetch = _http_json) -> list[Bar]:
    out: list[Bar] = []
    t = start
    while t < now:
        q = urllib.parse.urlencode({"symbol": symbol, "interval": "4h", "startTime": int(t.timestamp() * 1000), "limit": 1000})
        page = parse_binance(fetch(f"{BINANCE}?{q}"), now)
        if not page:
            break
        out.extend(page)
        t = page[-1].open_time + H4
    return out


# --- Bybit v5: GET /v5/market/kline?category=spot&symbol=BTCUSDT&interval=240&start=<ms>&limit=1000 ---
BYBIT = "https://api.bybit.com/v5/market/kline"


def parse_bybit(payload: dict, now: datetime) -> list[Bar]:
    if payload.get("retCode") != 0:
        raise RuntimeError(f"bybit error: {payload.get('retMsg')}")
    rows = payload["result"]["list"]  # newest first
    bars = [Bar(_utc(int(r[0]) / 1000), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])) for r in rows]
    return _complete(sorted(bars, key=lambda b: b.open_time), now)


def fetch_bybit(symbol: str, start: datetime, now: datetime, category: str = "spot", fetch: Fetch = _http_json) -> list[Bar]:
    out: list[Bar] = []
    t = start
    while t < now:
        q = urllib.parse.urlencode({"category": category, "symbol": symbol, "interval": 240,
                                    "start": int(t.timestamp() * 1000), "limit": 1000})
        page = parse_bybit(fetch(f"{BYBIT}?{q}"), now)
        page = [b for b in page if b.open_time >= t]
        if not page:
            break
        out.extend(page)
        t = page[-1].open_time + H4
    return out


def convert_quote(bars: list[Bar], rate: dict[datetime, float]) -> list[Bar]:
    """Re-quote USDT bars into USD with a certified USDT/USD close per bar; stablecoins are never assumed at par."""
    out = []
    for b in bars:
        r = rate.get(b.open_time)
        if r is None:
            continue
        out.append(Bar(b.open_time, b.o * r, b.h * r, b.l * r, b.c * r, b.v))
    return out
