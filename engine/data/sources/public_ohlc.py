"""Public market-data sources for 4h OHLCV (spec §8.1: Binance and Bybit are data sources; Kraken executes).

Bitstamp (native USD pairs) is a second reference source for hosts where Binance and Bybit refuse the caller's
location; `venue_sources` drops an unreachable venue and names it, and certification still needs >= 2 sources.

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


ALLOWED_HOSTS = ("https://api.kraken.com/", "https://api.binance.com/", "https://api.bybit.com/", "https://www.bitstamp.net/")


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


# --- Bitstamp: GET /api/v2/ohlc/btcusd/?step=14400&start=<unix s>&limit=1000 (USD-quoted, no re-quote) ---
BITSTAMP = "https://www.bitstamp.net/api/v2/ohlc"


def parse_bitstamp(payload: dict, now: datetime) -> list[Bar]:
    if "data" not in payload:
        raise RuntimeError(f"bitstamp error: {payload.get('errors') or payload.get('reason') or payload}")
    rows = payload["data"]["ohlc"]
    bars = [Bar(_utc(int(r["timestamp"])), float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]),
                float(r["volume"])) for r in rows]
    return _complete(sorted(bars, key=lambda b: b.open_time), now)


def fetch_bitstamp(pair: str, start: datetime, now: datetime, fetch: Fetch = _http_json) -> list[Bar]:
    out: list[Bar] = []
    t = start
    while t < now:
        q = urllib.parse.urlencode({"step": 14400, "start": int(t.timestamp()), "limit": 1000})
        page = [b for b in parse_bitstamp(fetch(f"{BITSTAMP}/{pair}/?{q}"), now) if b.open_time >= t]
        if not page:
            break
        out.extend(page)
        t = page[-1].open_time + H4
    return out


def venue_sources(base: str, kraken_pair: str, since: datetime, now: datetime,
                  fetch: Fetch = _http_json) -> tuple[dict[str, list[Bar]], dict[str, str]]:
    """Per-venue 4h bars for <base>/USD, Kraken first (the primary). A reference venue that refuses the request
    (geo-block, outage) is left out and reported in the second dict; Kraken failing is fatal."""
    out = {"kraken-spot": fetch_kraken(kraken_pair, since, now, fetch=fetch)}
    skipped: dict[str, str] = {}
    usdt: dict[datetime, float] | None = None

    def usdt_rate() -> dict[datetime, float]:
        nonlocal usdt
        if usdt is None:
            usdt = {b.open_time: b.c for b in fetch_kraken("USDTUSD", since, now, fetch=fetch)}
        return usdt

    venues = [
        ("bitstamp", lambda: fetch_bitstamp(f"{base.lower()}usd", since, now, fetch=fetch)),
        ("binance-spot", lambda: convert_quote(fetch_binance(f"{base}USDT", since, now, fetch=fetch), usdt_rate())),
        ("bybit-v5-spot", lambda: convert_quote(fetch_bybit(f"{base}USDT", since, now, fetch=fetch), usdt_rate())),
    ]
    for name, get in venues:
        try:
            out[name] = get()
        except (OSError, RuntimeError) as e:  # HTTPError/URLError are OSErrors
            skipped[name] = f"{type(e).__name__}: {e}"
    return out, skipped


def convert_quote(bars: list[Bar], rate: dict[datetime, float]) -> list[Bar]:
    """Re-quote USDT bars into USD with a certified USDT/USD close per bar; stablecoins are never assumed at par."""
    out = []
    for b in bars:
        r = rate.get(b.open_time)
        if r is None:
            continue
        out.append(Bar(b.open_time, b.o * r, b.h * r, b.l * r, b.c * r, b.v))
    return out
