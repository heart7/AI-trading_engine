"""Order and fill types shared by the OMS, adapters and the simulated venue (spec §8.2, §8.7)."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class OrderType(StrEnum):
    POST_ONLY = "POST_ONLY"  # limit, maker only
    IOC = "IOC"  # limit, immediate-or-cancel
    STOP = "STOP"  # venue-resident protective stop (reduce-only where supported)
    MARKET = "MARKET"  # protective exits and FLATTEN only (INV-30)


class Purpose(StrEnum):
    ENTRY = "ENTRY"
    ADD = "ADD"
    EXIT = "EXIT"  # protective exit, time stop, signal exit
    FLATTEN = "FLATTEN"
    PROTECT = "PROTECT"  # the venue-resident stop itself


ENTRY_PURPOSES = frozenset({Purpose.ENTRY, Purpose.ADD})


class OrderStatus(StrEnum):
    PENDING = "PENDING"  # created locally, not yet acknowledged
    OPEN = "OPEN"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"  # venue state could not be established; reconciliation decides (INV-40)


TERMINAL = frozenset({OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED})


@dataclass(frozen=True)
class OrderRequest:
    client_id: str
    instrument_id: str
    side: str  # "buy" | "sell"
    type: OrderType
    qty: float
    purpose: Purpose
    price: float | None = None  # limit price (POST_ONLY, IOC)
    stop_price: float | None = None  # STOP trigger
    reduce_only: bool = False


@dataclass
class VenueOrder:
    """What the venue reports for an order."""

    client_id: str
    venue_order_id: str
    instrument_id: str
    side: str
    type: OrderType
    qty: float
    filled: float
    status: OrderStatus
    price: float | None = None
    stop_price: float | None = None
    reduce_only: bool = False


@dataclass(frozen=True)
class Fill:
    client_id: str
    instrument_id: str
    side: str
    qty: float
    price: float
    fee: float
    liquidity: str  # "maker" | "taker"
    seq: int = 0


@dataclass
class Snapshot:
    """REST snapshot used by restart reconciliation and websocket-gap resync."""

    open_orders: list[VenueOrder] = field(default_factory=list)
    balances: dict[str, float] = field(default_factory=dict)  # asset -> total
    seq: int = 0
