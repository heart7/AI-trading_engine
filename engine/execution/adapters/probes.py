"""Permission-probe parsers (spec §8.5.3). Field names are ASSUMED from venue docs (A-PERMISSION-PROBES).

A parser that cannot find a field it needs reports the key as withdrawal-capable and trade-capable, so an
unexpected response shape is refused, never waved through.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from engine.execution.venues import PermissionProbe


def binance_api_restrictions(resp: Mapping[str, Any]) -> PermissionProbe:
    """GET /sapi/v1/account/apiRestrictions."""
    def flag(k: str, default: bool = True) -> bool:
        return bool(resp.get(k, default))
    return PermissionProbe(read=flag("enableReading", False), trade=flag("enableSpotAndMarginTrading"),
                           withdraw=flag("enableWithdrawals") or flag("enableInternalTransfer", False),
                           deriv_trade=flag("enableFutures"), ip_restricted=flag("ipRestrict", False),
                           trust="VERIFIED", evidence="binance apiRestrictions")


def bybit_query_api(resp: Mapping[str, Any]) -> PermissionProbe:
    """GET /v5/user/query-api."""
    if resp.get("retCode") != 0 or "result" not in resp:
        return PermissionProbe(read=False, trade=True, withdraw=True, deriv_trade=True, evidence="bybit probe failed")
    r = resp["result"]
    perms: Mapping[str, list[str]] = r.get("permissions", {})
    read_only = r.get("readOnly", 0) == 1
    wallet = [p.lower() for p in perms.get("Wallet", [])]
    withdraw = any("withdraw" in p for p in wallet)
    spot = bool(perms.get("Spot")) and not read_only
    deriv = (bool(perms.get("ContractTrade")) or bool(perms.get("Derivatives"))) and not read_only
    ips = r.get("ips", ["*"])
    return PermissionProbe(read=True, trade=spot, withdraw=withdraw, deriv_trade=deriv,
                           ip_restricted=ips not in (["*"], [], None), trust="VERIFIED", evidence="bybit query-api")


def kraken_attested(*, attested: Mapping[str, bool], withdraw_negative_probe_error: str | None) -> PermissionProbe:
    """Kraken exposes no key-permission endpoint. The principal attests the permission set (screenshot kept as
    evidence) and a withdrawal-info call to a non-whitelisted address must fail with a permission error."""
    denied = bool(withdraw_negative_probe_error) and "Permission denied" in withdraw_negative_probe_error
    return PermissionProbe(read=bool(attested.get("query_funds")), trade=bool(attested.get("create_modify_orders")),
                           withdraw=bool(attested.get("withdraw_funds")) or not denied,
                           trust="ATTESTED", evidence="principal attestation + negative withdrawal probe")
