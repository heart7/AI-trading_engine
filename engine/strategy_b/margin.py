"""Strategy B sizing, margin and leverage (spec §6.6, INV-14, INV-15).

Isolated margin is structural: `PerpOrder.margin_mode` accepts only ISOLATED and there is no other code path.
The primary invariant is simultaneous liquidation: sum of posted margin <= phi x hard DD budget. An order that
would breach it is refused with MARGIN_INVARIANT.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal


class MarginInvariant(Exception):
    code = "MARGIN_INVARIANT"


@dataclass(frozen=True)
class PerpOrder:
    instrument_id: str
    side: int  # +1 long, -1 short
    notional: float  # share of NAV
    leverage: float
    margin_mode: Literal["ISOLATED"] = "ISOLATED"

    def __post_init__(self) -> None:
        if self.margin_mode != "ISOLATED":
            raise ValueError("isolated margin only; cross margin has no code path (INV-15)")

    @property
    def margin(self) -> float:
        return self.notional / self.leverage


@dataclass(frozen=True)
class PerpPosition:
    instrument_id: str
    side: int
    notional: float
    leverage: float

    @property
    def margin(self) -> float:
        return self.notional / self.leverage


def max_leverage(policy: Mapping[str, Any], stop_distance: float, mmr: float) -> float:
    """Leverage such that liquidation distance >= max(x_stop x stop distance, min_distance), capped (§6.6)."""
    lb = policy["perps"]["liq_buffer"]
    need = max(lb["x_stop"] * stop_distance, lb["min_distance"])
    return min(policy["perps"]["max_eff_leverage"], 1.0 / (need + mmr))


def liquidation_distance(leverage: float, mmr: float) -> float:
    return max(0.0, 1.0 / leverage - mmr)


def margin_cap(policy: Mapping[str, Any]) -> float:
    return policy["perps"]["phi"] * policy["risk"]["ladder"]["dd"]["terminate"]


def check_order(policy: Mapping[str, Any], book: Sequence[PerpPosition], order: PerpOrder) -> None:
    posted = sum(p.margin for p in book)
    if posted + order.margin > margin_cap(policy) + 1e-12:
        raise MarginInvariant(f"posted margin {posted + order.margin:.4f} NAV > cap {margin_cap(policy):.4f}")
    gross = sum(p.notional for p in book) + order.notional
    if gross > policy["perps"]["gross_max"] + 1e-12:
        raise MarginInvariant(f"gross {gross:.3f} > {policy['perps']['gross_max']}")
    net = sum(p.side * p.notional for p in book) + order.side * order.notional
    if abs(net) > policy["perps"]["net_directional_max"] + 1e-12:
        raise MarginInvariant(f"net directional {net:.3f} beyond {policy['perps']['net_directional_max']}")


def size_b(policy: Mapping[str, Any], *, r_b: float, stop_distance: float, mmr: float, book: Sequence[PerpPosition],
           size_es: float = float("inf"), size_map: float = float("inf"), v: float = 1.0) -> dict[str, float]:
    """size_B = min(r_B / d, size_ES, size_liq, size_map, size_margin) x v, in NAV share of notional."""
    lev = max_leverage(policy, stop_distance, mmr)
    headroom = max(0.0, margin_cap(policy) - sum(p.margin for p in book))
    parts = {"risk": r_b / stop_distance, "es": size_es, "liq": lev * 1.0, "map": size_map, "margin": headroom * lev}
    binding = min(parts, key=parts.get)
    return {"notional": min(parts.values()) * min(v, 1.0), "leverage": lev, "binding": binding, **parts}
