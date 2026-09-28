"""NAV computed two ways and reconciled (spec §12.1).

Ledger path: fund-held balances (cash, coins, off-venue) from the ledger, valued at certified prices.
Venue path: balances reported by each venue plus off-venue balances, valued at the same prices.
Divergence above 0.1% (D) marks NAV provisional and blocks entries (NAV_DIVERGENCE).
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, localcontext

from engine.common.incidents import IncidentLog
from engine.evidence.ledger import D0, EXACT, Ledger, dec

HELD_PREFIXES = ("cash:", "asset:", "offvenue:")
NAV_DIVERGENCE_MAX = Decimal("0.001")


@dataclass(frozen=True)
class NavSnapshot:
    ts: str
    ledger_nav: Decimal
    venue_nav: Decimal
    divergence: Decimal
    verified: bool


def value(balances: Mapping[str, Decimal], prices_usd: Mapping[str, float]) -> Decimal:
    with localcontext(EXACT):
        return _value(balances, prices_usd)


def _value(balances: Mapping[str, Decimal], prices_usd: Mapping[str, float]) -> Decimal:
    total = D0
    for ccy, amt in balances.items():
        if amt == 0:
            continue
        if ccy not in prices_usd:
            raise KeyError(f"no certified USD price for {ccy}")
        total += dec(amt) * dec(prices_usd[ccy])
    return total


def ledger_holdings(ledger: Ledger) -> dict[str, Decimal]:
    out: dict[str, Decimal] = {}
    for (acct, ccy), amt in ledger.balances().items():
        if acct.startswith(HELD_PREFIXES):
            out[ccy] = out.get(ccy, D0) + amt
    return out


def reconcile_nav(ledger: Ledger, venue_balances: Mapping[str, Mapping[str, float]], offvenue: Mapping[str, float],
                  prices_usd: Mapping[str, float], *, ts: str, incidents: IncidentLog | None = None) -> NavSnapshot:
    lnav = value(ledger_holdings(ledger), prices_usd)
    agg: dict[str, Decimal] = {}
    for bal in list(venue_balances.values()) + [offvenue]:
        for ccy, amt in bal.items():
            agg[ccy] = agg.get(ccy, D0) + dec(amt)
    vnav = value(agg, prices_usd)
    base = max(abs(lnav), abs(vnav), Decimal(1))
    div = abs(lnav - vnav) / base
    ok = div <= NAV_DIVERGENCE_MAX
    if incidents is not None:
        if ok:
            incidents.resolve("NAV_DIVERGENCE", "engine", f"divergence {div:.6f}")
        elif "NAV_DIVERGENCE" not in incidents.open_codes("engine"):
            incidents.open_incident("NAV_DIVERGENCE", "S2", "engine", f"ledger {lnav} vs venue {vnav} ({div:.4%})")
    return NavSnapshot(ts, lnav, vnav, div, ok)
