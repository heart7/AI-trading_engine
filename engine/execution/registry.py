"""Connector registry (spec §8.5.1). A connector type is code; a venue instance is configuration.

Connector types are registered here. Venue instances of a registered type are created at
runtime from Settings -> Exchanges, so adding or re-keying an account never needs a deploy.
`ccxt-generic` is market data and PAPER only and can never be trade-enabled (INV-38).
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace


@dataclass(frozen=True)
class ConnectorType:
    connector_type: str
    venue: str
    products: tuple[str, ...]  # "spot" | "perp"
    environments: tuple[str, ...]  # "testnet" | "demo" | "live"
    auth_scheme: str
    permission_probe: str  # how key permissions are established
    capability_fetch: str
    rate_limit_model: str
    stop_orders: bool
    dead_man_switch: str | None  # venue feature name, None when absent
    trust_cap: str = "VERIFIED"  # VERIFIED (probe endpoint) | ATTESTED (principal attests, e.g. Kraken)
    trade_capable: bool = True
    version: str = "0.1.0"
    conformance: Mapping[str, str] = field(default_factory=dict)  # {"version":..., "run_id":...} of the last PASS


# Probe endpoints and dead-man features are ASSUMED from venue documentation (spec §8.5.3, §8.7); verify at P3 go-live.
SHIPPED: tuple[ConnectorType, ...] = (
    ConnectorType("binance-spot", "binance", ("spot",), ("testnet", "live"), "hmac-sha256",
                  "GET /sapi/v1/account/apiRestrictions", "GET /api/v3/exchangeInfo + /sapi/v1/asset/tradeFee",
                  "weight-per-minute", True, "cancelReplace/none"),
    ConnectorType("binance-usdm", "binance", ("perp",), ("testnet", "live"), "hmac-sha256",
                  "GET /sapi/v1/account/apiRestrictions", "GET /fapi/v1/exchangeInfo", "weight-per-minute", True,
                  "countdownCancelAll"),
    ConnectorType("bybit-v5-spot", "bybit", ("spot",), ("testnet", "demo", "live"), "hmac-sha256",
                  "GET /v5/user/query-api", "GET /v5/market/instruments-info?category=spot", "per-endpoint-uid", True,
                  "disconnect-cancel-all"),
    ConnectorType("bybit-v5-linear", "bybit", ("perp",), ("testnet", "demo", "live"), "hmac-sha256",
                  "GET /v5/user/query-api", "GET /v5/market/instruments-info?category=linear", "per-endpoint-uid", True,
                  "disconnect-cancel-all"),
    ConnectorType("kraken-spot", "kraken", ("spot",), ("demo", "live"), "hmac-sha512",
                  "attested + negative probes", "GET /0/public/AssetPairs + /0/private/TradeVolume", "call-counter-decay",
                  True, "CancelAllOrdersAfter", trust_cap="ATTESTED"),
    ConnectorType("kraken-futures", "kraken", ("perp",), ("demo", "live"), "hmac-sha512",
                  "attested + negative probes", "GET /derivatives/api/v3/instruments", "cost-per-10s", True,
                  "cancelallordersafter", trust_cap="ATTESTED"),
    ConnectorType("ccxt-generic", "any", ("spot", "perp"), ("live",), "varies", "none", "ccxt.load_markets",
                  "ccxt-default", False, None, trust_cap="ATTESTED", trade_capable=False),
)


class UnknownConnector(KeyError):
    pass


@dataclass
class ConnectorRegistry:
    types: dict[str, ConnectorType]

    @classmethod
    def shipped(cls) -> ConnectorRegistry:
        return cls({c.connector_type: c for c in SHIPPED})

    def get(self, connector_type: str) -> ConnectorType:
        try:
            return self.types[connector_type]
        except KeyError as e:
            raise UnknownConnector(f"{connector_type} has no connector; adding a new exchange type needs an adapter "
                                   "that passes the conformance suite") from e

    def record_conformance(self, connector_type: str, version: str, run_id: str) -> None:
        c = self.get(connector_type)
        self.types[connector_type] = replace(c, conformance={"version": version, "run_id": run_id})
