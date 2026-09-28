"""Collateral and treasury policy (spec §8.4, INV-16).

Hard checks, evaluated on every cycle: per-venue exposure cap, per-issuer stablecoin cap, stablecoin par band,
off-exchange reserve. A breach blocks entries on the venue in question (or engine-wide for the reserve).
Sweeps are `transfer_intent`s the engine proposes and a human executes with keys the engine does not hold.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from engine.common.canonical import content_hash
from engine.common.incidents import IncidentLog


@dataclass(frozen=True)
class TransferIntent:
    frm: str
    to: str
    ccy: str
    amount: float
    reason: str
    executed_by: str = "human"  # always; the engine holds no withdrawal-scope credentials (INV-02)

    @property
    def intent_id(self) -> str:
        return "ti-" + content_hash({"frm": self.frm, "to": self.to, "ccy": self.ccy, "amount": round(self.amount, 2),
                                     "reason": self.reason})[:16]


@dataclass
class CollateralReport:
    blocked_venues: dict[str, list[str]] = field(default_factory=dict)
    engine_blocks: list[str] = field(default_factory=list)
    reduce_only_stables: list[str] = field(default_factory=list)
    sweeps: list[TransferIntent] = field(default_factory=list)
    headroom: dict[str, float] = field(default_factory=dict)  # venue -> cap - exposure (share of NAV)


def check_collateral(policy: Mapping[str, Any], *, nav: float, venue_holdings_usd: Mapping[str, Mapping[str, float]],
                     venue_cash_usd: Mapping[str, float], stable_holdings_usd: Mapping[str, float],
                     stable_prices: Mapping[str, float], offvenue_usd: float, bank_label: str = "bank",
                     incidents: IncidentLog | None = None) -> CollateralReport:
    col, cap = policy["collateral"], policy["risk"]["venue_exposure_max"]
    rep = CollateralReport()
    for venue, holdings in venue_holdings_usd.items():
        exposure = (sum(holdings.values()) + venue_cash_usd.get(venue, 0.0)) / nav
        rep.headroom[venue] = cap - exposure
        if exposure > cap + 1e-12:
            rep.blocked_venues.setdefault(venue, []).append(f"VENUE_EXPOSURE {exposure:.3f} > {cap}")
        buffer = col["spot_operating_buffer"] * nav
        excess = venue_cash_usd.get(venue, 0.0) - buffer
        if excess > 0.01 * nav:
            rep.sweeps.append(TransferIntent(f"cash:{venue}:USD", f"offvenue:{bank_label}:USD", "USD", round(excess, 2),
                                             "cash above operating buffer (spec §8.4)"))
    for issuer, usd in stable_holdings_usd.items():
        if usd / nav > col["issuer_cap"] + 1e-12:
            rep.engine_blocks.append(f"ISSUER_CAP {issuer} {usd / nav:.3f} > {col['issuer_cap']}")
        px = stable_prices.get(issuer, 1.0)
        if abs(px - 1.0) > col["par_band"]:
            rep.reduce_only_stables.append(issuer)
    if offvenue_usd / nav < col["off_exchange_min"] - 1e-12:
        rep.engine_blocks.append(f"OFF_EXCHANGE_RESERVE {offvenue_usd / nav:.3f} < {col['off_exchange_min']}")
    if incidents is not None:
        for venue, why in rep.blocked_venues.items():
            incidents.open_incident("COLLATERAL_CAP", "S2", f"venue:{venue}", "; ".join(why))
        for why in rep.engine_blocks:
            incidents.open_incident("COLLATERAL_CAP", "S2", "engine", why)
        for s in rep.reduce_only_stables:
            incidents.open_incident("STABLECOIN_DEPEG", "S2", "engine", f"{s} outside par band; reduce-only")
    return rep
