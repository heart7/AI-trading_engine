"""Kraken spot REST adapter (connector `kraken-spot`). Status: implemented, unverified against the live API.

Request signing follows Kraken's documented scheme (HMAC-SHA512 over path + SHA256(nonce + body), key base64)
and is tested against Kraken's published example. Endpoint parameters and error codes are ASSUMED from the
public docs (A-KRAKEN-ERRORS) and must be re-checked by the conformance suite on Kraken's demo environment.

The transport is injected. There is no default transport, so constructing this adapter cannot reach the
network by accident; PAPER mode refuses any non-simulated adapter in the OMS as a second line.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import urllib.parse
from collections.abc import Callable, Mapping
from typing import Any

from engine.execution.errors import ErrorClass, Timeout, VenueError, classify
from engine.execution.model import Fill, OrderRequest, OrderStatus, OrderType, Snapshot, VenueOrder
from engine.secrets.store import SecretValue

API = "https://api.kraken.com"

ERRORS: dict[str, ErrorClass] = {
    "EAPI:Invalid nonce": ErrorClass.RETRYABLE,
    "EAPI:Rate limit exceeded": ErrorClass.RETRYABLE,
    "EOrder:Rate limit exceeded": ErrorClass.RETRYABLE,
    "EGeneral:Temporary lockout": ErrorClass.RETRYABLE,
    "EOrder:Insufficient funds": ErrorClass.REJECTED,
    "EOrder:Order minimum not met": ErrorClass.REJECTED,
    "EOrder:Cost minimum not met": ErrorClass.REJECTED,
    "EOrder:Post only order": ErrorClass.REJECTED,
    "EOrder:Unknown order": ErrorClass.REJECTED,
    "EOrder:Invalid price*": ErrorClass.REJECTED,
    "EGeneral:Invalid arguments*": ErrorClass.REJECTED,
    "EGeneral:Permission denied": ErrorClass.REJECTED,
    "EAPI:Invalid key": ErrorClass.REJECTED,
    "EAPI:Invalid signature": ErrorClass.REJECTED,
    "EService:Unavailable": ErrorClass.UNKNOWN_STATE,
    "EService:Busy": ErrorClass.UNKNOWN_STATE,
    "EService:Deadline elapsed": ErrorClass.UNKNOWN_STATE,
    "EGeneral:Internal error": ErrorClass.UNKNOWN_STATE,
}

STATUS = {"pending": OrderStatus.PENDING, "open": OrderStatus.OPEN, "closed": OrderStatus.FILLED,
          "canceled": OrderStatus.CANCELLED, "expired": OrderStatus.CANCELLED}

Transport = Callable[[str, str, Mapping[str, str], bytes | None], tuple[int, dict[str, Any]]]


def sign(path: str, nonce: str, body: str, secret_b64: str) -> str:
    msg = path.encode() + hashlib.sha256((nonce + body).encode()).digest()
    return base64.b64encode(hmac.new(base64.b64decode(secret_b64), msg, hashlib.sha512).digest()).decode()


class KrakenSpotAdapter:
    connector_type = "kraken-spot"
    connector_version = "0.1.0"
    paper = False
    supports_amend = True

    def __init__(self, venue_id: str, environment: str, *, api_key: SecretValue, api_secret: SecretValue,
                 transport: Transport, nonce: Callable[[], int], pairs: Mapping[str, str]):
        self.venue_id, self.environment = venue_id, environment
        self._key, self._secret = api_key, api_secret
        self._transport, self._nonce = transport, nonce
        self.pairs = dict(pairs)  # instrument_id -> Kraken pair name (e.g. BTC-USD -> XBTUSD)
        self._inst = {v: k for k, v in self.pairs.items()}

    # -- plumbing --------------------------------------------------------------------------------
    def _call(self, method: str, url: str, headers: Mapping[str, str], body: bytes | None) -> dict[str, Any]:
        try:
            status, resp = self._transport(method, url, headers, body)
        except TimeoutError as e:
            raise Timeout(str(e)) from e
        if status >= 500:
            raise VenueError(ErrorClass.UNKNOWN_STATE, f"HTTP_{status}")
        errs = resp.get("error") or []
        if errs:
            raise VenueError(classify(ERRORS, errs[0]), errs[0])
        return resp.get("result", {})

    def _private(self, endpoint: str, params: Mapping[str, Any]) -> dict[str, Any]:
        path = f"/0/private/{endpoint}"
        nonce = str(self._nonce())
        body = urllib.parse.urlencode({"nonce": nonce, **{k: v for k, v in params.items() if v is not None}})
        headers = {"API-Key": self._key.reveal(), "API-Sign": sign(path, nonce, body, self._secret.reveal()),
                   "Content-Type": "application/x-www-form-urlencoded"}
        return self._call("POST", API + path, headers, body.encode())

    # -- request builders (pure; tested directly) ------------------------------------------------
    def order_params(self, req: OrderRequest) -> dict[str, Any]:
        p: dict[str, Any] = {"pair": self.pairs[req.instrument_id], "type": req.side, "volume": f"{req.qty:.8f}",
                             "cl_ord_id": req.client_id}
        if req.type == OrderType.POST_ONLY:
            p.update(ordertype="limit", price=f"{req.price}", oflags="post")
        elif req.type == OrderType.IOC:
            p.update(ordertype="limit", price=f"{req.price}", timeinforce="IOC")
        elif req.type == OrderType.STOP:
            p.update(ordertype="stop-loss", price=f"{req.stop_price}", trigger="last")
        elif req.type == OrderType.MARKET:
            p.update(ordertype="market")
        return p

    # -- adapter interface -----------------------------------------------------------------------
    def server_time_ms(self) -> int:
        r = self._call("GET", API + "/0/public/Time", {}, None)
        return int(r["unixtime"]) * 1000

    def place(self, req: OrderRequest) -> VenueOrder:
        r = self._private("AddOrder", self.order_params(req))
        txid = (r.get("txid") or [""])[0]
        return VenueOrder(req.client_id, txid, req.instrument_id, req.side, req.type, req.qty, 0.0, OrderStatus.OPEN,
                          req.price, req.stop_price, req.reduce_only)

    def amend(self, client_id: str, *, qty: float | None = None, stop_price: float | None = None) -> VenueOrder:
        self._private("AmendOrder", {"cl_ord_id": client_id, "order_qty": None if qty is None else f"{qty:.8f}",
                                     "trigger_price": stop_price})
        vo = self.query(client_id)
        if vo is None:
            raise VenueError(ErrorClass.UNKNOWN_STATE, "AMEND_READBACK_MISSING")
        return vo

    def cancel(self, client_id: str) -> VenueOrder:
        self._private("CancelOrder", {"cl_ord_id": client_id})
        vo = self.query(client_id)
        if vo is None:
            raise VenueError(ErrorClass.UNKNOWN_STATE, "CANCEL_READBACK_MISSING")
        return vo

    def _to_order(self, txid: str, o: Mapping[str, Any]) -> VenueOrder:
        d = o.get("descr", {})
        otype = {"stop-loss": OrderType.STOP, "market": OrderType.MARKET}.get(d.get("ordertype"), OrderType.POST_ONLY
                                                                             if "post" in o.get("oflags", "") else OrderType.IOC)
        price = float(d.get("price") or 0) or None
        return VenueOrder(o.get("cl_ord_id", ""), txid, self._inst.get(d.get("pair", ""), d.get("pair", "")),
                          d.get("type", ""), otype, float(o.get("vol", 0)), float(o.get("vol_exec", 0)),
                          STATUS.get(o.get("status", ""), OrderStatus.UNKNOWN),
                          None if otype == OrderType.STOP else price, price if otype == OrderType.STOP else None)

    def query(self, client_id: str) -> VenueOrder | None:
        for endpoint, key in (("OpenOrders", "open"), ("ClosedOrders", "closed")):
            r = self._private(endpoint, {"cl_ord_id": client_id})
            for txid, o in (r.get(key) or {}).items():
                if o.get("cl_ord_id") == client_id:
                    return self._to_order(txid, o)
        return None

    def snapshot(self) -> Snapshot:
        opens = self._private("OpenOrders", {}).get("open") or {}
        bal = self._private("Balance", {})
        return Snapshot([self._to_order(t, o) for t, o in opens.items()], {k: float(v) for k, v in bal.items()}, 0)

    def fills_since(self, seq: int) -> list[Fill]:
        r = self._private("TradesHistory", {"start": seq / 1e6 if seq else None})
        out = []
        for _tid, t in sorted((r.get("trades") or {}).items(), key=lambda kv: kv[1]["time"]):
            s = int(float(t["time"]) * 1e6)
            if s > seq:
                out.append(Fill(t.get("cl_ord_id", t.get("ordertxid", "")), self._inst.get(t["pair"], t["pair"]), t["type"],
                                float(t["vol"]), float(t["price"]), float(t["fee"]),
                                "maker" if t.get("maker") else "taker", s))
        return out

    def cancel_all_after(self, seconds: int) -> None:
        self._private("CancelAllOrdersAfter", {"timeout": seconds})
