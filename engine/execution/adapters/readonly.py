"""Read-only account adapters for Binance spot and Bybit v5 (spec §8.1, P3). Status: implemented, unverified live.

These venues are price sources and tax-history sources for this owner (INV-43: no trading without the
venue's written confirmation that a UK resident may use the account). The classes have no order methods at
all, so there is nothing to misroute; `place` exists only to fail loudly if something tries.
Signing follows each venue's documented scheme; Binance's is tested against its published example.
"""
from __future__ import annotations

import hashlib
import hmac
import urllib.parse
from collections.abc import Callable, Mapping
from typing import Any

from engine.execution.errors import ErrorClass, VenueError
from engine.secrets.store import SecretValue

Transport = Callable[[str, str, Mapping[str, str], bytes | None], tuple[int, Any]]


def binance_sign(query: str, secret: str) -> str:
    return hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()


def bybit_sign(timestamp_ms: int, api_key: str, recv_window: int, payload: str, secret: str) -> str:
    return hmac.new(secret.encode(), f"{timestamp_ms}{api_key}{recv_window}{payload}".encode(), hashlib.sha256).hexdigest()


class _ReadOnly:
    paper = False
    trade_capable = False

    def place(self, *_a: Any, **_k: Any) -> None:
        raise VenueError(ErrorClass.REJECTED, "READ_ONLY_CONNECTOR", f"{self.connector_type} is data-only for this owner")

    amend = cancel = cancel_all_after = place


class BinanceSpotReadOnly(_ReadOnly):
    connector_type = "binance-spot"
    BASE = "https://api.binance.com"

    def __init__(self, *, api_key: SecretValue, api_secret: SecretValue, transport: Transport, clock_ms: Callable[[], int]):
        self._k, self._s, self._t, self._clock = api_key, api_secret, transport, clock_ms

    def _signed(self, path: str, params: Mapping[str, Any]) -> Any:
        q = urllib.parse.urlencode({**params, "recvWindow": 5000, "timestamp": self._clock()})
        url = f"{self.BASE}{path}?{q}&signature={binance_sign(q, self._s.reveal())}"
        status, body = self._t("GET", url, {"X-MBX-APIKEY": self._k.reveal()}, None)
        if status != 200:
            raise VenueError(ErrorClass.UNKNOWN_STATE if status >= 500 else ErrorClass.REJECTED, f"HTTP_{status}")
        return body

    def api_restrictions(self) -> Mapping[str, Any]:
        return self._signed("/sapi/v1/account/apiRestrictions", {})

    def my_trades(self, symbol: str, from_id: int | None = None) -> list[Mapping[str, Any]]:
        return self._signed("/api/v3/myTrades", {"symbol": symbol, **({"fromId": from_id} if from_id else {})})

    def deposits(self, start_ms: int) -> list[Mapping[str, Any]]:
        return self._signed("/sapi/v1/capital/deposit/hisrec", {"startTime": start_ms})


class BybitV5ReadOnly(_ReadOnly):
    connector_type = "bybit-v5-spot"
    BASE = "https://api.bybit.com"

    def __init__(self, *, api_key: SecretValue, api_secret: SecretValue, transport: Transport, clock_ms: Callable[[], int]):
        self._k, self._s, self._t, self._clock = api_key, api_secret, transport, clock_ms

    def _signed(self, path: str, params: Mapping[str, Any]) -> Any:
        q = urllib.parse.urlencode(params)
        ts, rw = self._clock(), 5000
        headers = {"X-BAPI-API-KEY": self._k.reveal(), "X-BAPI-TIMESTAMP": str(ts), "X-BAPI-RECV-WINDOW": str(rw),
                   "X-BAPI-SIGN": bybit_sign(ts, self._k.reveal(), rw, q, self._s.reveal())}
        status, body = self._t("GET", f"{self.BASE}{path}?{q}", headers, None)
        if status != 200 or body.get("retCode") != 0:
            raise VenueError(ErrorClass.REJECTED, f"BYBIT_{body.get('retCode') if isinstance(body, dict) else status}")
        return body

    def query_api(self) -> Mapping[str, Any]:
        return self._signed("/v5/user/query-api", {})

    def executions(self, category: str = "spot", cursor: str | None = None) -> Mapping[str, Any]:
        return self._signed("/v5/execution/list", {"category": category, **({"cursor": cursor} if cursor else {})})
