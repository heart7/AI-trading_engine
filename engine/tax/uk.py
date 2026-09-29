"""UK capital gains for spot crypto (spec §12.4, §12.7). ASSUMED rules; not tax advice; accountant sign-off before LIVE.

The fund runs in USD. GBP appears only here: every acquisition and disposal is converted at the certified GBP/USD
rate for its UTC date. Matching follows the published HMRC share-matching order for cryptoassets, per asset and
across every account the owner holds (Kraken, and the read-only Binance and Bybit records):

  1. same day      disposals are matched with acquisitions of the same asset on the same day
  2. 30 days       then with acquisitions in the 30 days after the disposal, earliest first ("bed and breakfast")
  3. section 104   then from the pool at average allowable cost

Allowable costs include acquisition fees; disposal fees reduce proceeds. Tax years run 6 April to 5 April. The
reserve accrues at the declared rate on net gains above the declared annual exempt amount; while either is unset
(ASSUMED, awaiting the accountant) the reserve is reported as UNSET rather than as a number.
Historical results are never recomputed in place: a changed tax policy is a new version (§12.4).
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

import yaml

from engine.common.canonical import content_hash

TAX_POLICY_FILE = Path(__file__).resolve().parents[2] / "policy" / "tax" / "tax-uk-v1.yaml"
PENNY = Decimal("0.01")
BNB_DAYS = 30


class TaxInputError(Exception):
    pass


@dataclass(frozen=True)
class Transaction:
    """One fill or trade from any of the owner's accounts, valued in USD at execution."""
    ts: datetime
    account: str  # e.g. kraken-spot, binance-spot (read-only record), bybit-v5-spot (read-only record)
    asset: str
    side: str  # BUY | SELL
    qty: Decimal
    value_usd: Decimal  # gross consideration
    fee_usd: Decimal = Decimal(0)
    ref: str = ""


@dataclass
class Match:
    rule: str  # SAME_DAY | BED_AND_BREAKFAST | S104
    qty: Decimal
    cost_gbp: Decimal
    acquired_on: date | None


@dataclass
class Disposal:
    asset: str
    on: date
    qty: Decimal
    proceeds_gbp: Decimal
    matches: list[Match] = field(default_factory=list)

    @property
    def cost_gbp(self) -> Decimal:
        return sum((m.cost_gbp for m in self.matches), Decimal(0))

    @property
    def gain_gbp(self) -> Decimal:
        return self.proceeds_gbp - self.cost_gbp


def load_tax_policy(path: Path | None = None) -> dict[str, Any]:
    doc = yaml.safe_load(Path(path or TAX_POLICY_FILE).read_text())
    if doc.get("jurisdiction") != "UK" or doc.get("matching") != ["same_day", "30_day", "s104"]:
        raise TaxInputError("this engine implements the UK matching order only")
    return doc


def tax_year(d: date) -> str:
    start = d.year if (d.month, d.day) >= (4, 6) else d.year - 1
    return f"{start}-{str(start + 1)[2:]}"


def _gbp(usd: Decimal, on: date, fx: Mapping[date, Decimal]) -> Decimal:
    r = fx.get(on)
    if r is None:
        raise TaxInputError(f"no certified GBP/USD rate for {on.isoformat()}")
    return usd * r


@dataclass
class _Day:
    buy_qty: Decimal = Decimal(0)
    buy_cost: Decimal = Decimal(0)  # GBP incl. fees
    sell_qty: Decimal = Decimal(0)
    sell_proceeds: Decimal = Decimal(0)  # GBP net of fees


def compute(transactions: Iterable[Transaction], gbp_per_usd: Mapping[date, Decimal]) -> tuple[list[Disposal], dict[str, dict]]:
    """Match every disposal. Returns (disposals in date order, end pools per asset)."""
    per_asset: dict[str, dict[date, _Day]] = defaultdict(lambda: defaultdict(_Day))
    for t in transactions:
        if t.side not in ("BUY", "SELL") or t.qty <= 0:
            raise TaxInputError(f"bad transaction {t.ref or t}")
        d = t.ts.date()
        day = per_asset[t.asset][d]
        if t.side == "BUY":
            day.buy_qty += t.qty
            day.buy_cost += _gbp(t.value_usd + t.fee_usd, d, gbp_per_usd)
        else:
            day.sell_qty += t.qty
            day.sell_proceeds += _gbp(t.value_usd - t.fee_usd, d, gbp_per_usd)
    out: list[Disposal] = []
    pools: dict[str, dict] = {}
    for asset, days in sorted(per_asset.items()):
        disp, pool = _match_asset(asset, days)
        out += disp
        pools[asset] = pool
    return sorted(out, key=lambda x: (x.on, x.asset)), pools


def _match_asset(asset: str, days: Mapping[date, _Day]) -> tuple[list[Disposal], dict]:
    order = sorted(days)
    avail = {d: days[d].buy_qty for d in order}  # acquisition quantity not yet matched
    unit_cost = {d: (days[d].buy_cost / days[d].buy_qty) if days[d].buy_qty else Decimal(0) for d in order}
    disposals: dict[date, Disposal] = {}
    left: dict[date, Decimal] = {}
    # pass 1: the same-day rule for every disposal first, then the 30-day rule with disposals in date order
    for d in order:
        day = days[d]
        if not day.sell_qty:
            continue
        dp = Disposal(asset, d, day.sell_qty, day.sell_proceeds)
        q = min(day.sell_qty, avail[d])
        if q > 0:
            dp.matches.append(Match("SAME_DAY", q, unit_cost[d] * q, d))
            avail[d] -= q
        disposals[d], left[d] = dp, day.sell_qty - q
    for d in sorted(disposals):
        need = left[d]
        for a in order:
            if need <= 0:
                break
            if d < a <= d + timedelta(days=BNB_DAYS) and avail[a] > 0:
                q = min(need, avail[a])
                disposals[d].matches.append(Match("BED_AND_BREAKFAST", q, unit_cost[a] * q, a))
                avail[a] -= q
                need -= q
        left[d] = need
    # pass 2: the section 104 pool, walked in date order with only unmatched acquisitions entering it
    pool_qty, pool_cost = Decimal(0), Decimal(0)
    for d in order:
        if avail[d] > 0:
            pool_qty += avail[d]
            pool_cost += unit_cost[d] * avail[d]
        need = left.get(d, Decimal(0))
        if need > 0:
            if need > pool_qty:
                raise TaxInputError(f"{asset} {d.isoformat()}: disposal of {need} exceeds holdings {pool_qty}")
            cost = pool_cost * need / pool_qty
            disposals[d].matches.append(Match("S104", need, cost, None))
            pool_qty -= need
            pool_cost -= cost
    return [disposals[d] for d in sorted(disposals)], {"qty": pool_qty, "cost_gbp": pool_cost}


def _r(x: Decimal) -> Decimal:
    return x.quantize(PENNY, rounding=ROUND_HALF_UP)


def summary(disposals: Sequence[Disposal], policy: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Per tax year: gains, losses, net, taxable above the exempt amount, and the reserve (or UNSET)."""
    years: dict[str, dict[str, Any]] = {}
    for dp in disposals:
        y = years.setdefault(tax_year(dp.on), {"disposals": 0, "proceeds_gbp": Decimal(0), "gains_gbp": Decimal(0),
                                               "losses_gbp": Decimal(0)})
        y["disposals"] += 1
        y["proceeds_gbp"] += dp.proceeds_gbp
        g = dp.gain_gbp
        if g >= 0:
            y["gains_gbp"] += g
        else:
            y["losses_gbp"] += -g
    for ty, y in years.items():
        net = y["gains_gbp"] - y["losses_gbp"]
        cfg = policy.get("years", {}).get(ty, {})
        exempt, rate = cfg.get("annual_exempt_gbp"), cfg.get("reserve_rate")
        y["net_gbp"] = net
        if exempt is None or rate is None:
            y.update(taxable_gbp=None, reserve_gbp=None, reserve_status="UNSET (ASSUMED: rate or exempt amount not declared)")
        else:
            taxable = max(Decimal(0), net - Decimal(str(exempt)))
            y.update(taxable_gbp=taxable, reserve_gbp=taxable * Decimal(str(rate)), reserve_status="DECLARED")
        for k in ("proceeds_gbp", "gains_gbp", "losses_gbp", "net_gbp", "taxable_gbp", "reserve_gbp"):
            if y[k] is not None:
                y[k] = _r(y[k])
        y["loss_carry_forward"] = "not computed (ASSUMED: for the accountant)"
    return dict(sorted(years.items()))


EXPORT_COLUMNS = ["tax_year", "disposal_date", "asset", "quantity", "proceeds_gbp", "rule", "matched_quantity",
                  "acquired_on", "allowable_cost_gbp", "gain_gbp"]


def export_rows(disposals: Sequence[Disposal]) -> list[dict[str, str]]:
    """Per-disposal matching report an accountant can import (§12.7). One row per match; the gain sits on the
    disposal's last row so a column sum gives the total."""
    rows = []
    for dp in disposals:
        for i, m in enumerate(dp.matches):
            last = i == len(dp.matches) - 1
            rows.append({"tax_year": tax_year(dp.on), "disposal_date": dp.on.isoformat(), "asset": dp.asset,
                         "quantity": str(dp.qty), "proceeds_gbp": str(_r(dp.proceeds_gbp)) if i == 0 else "",
                         "rule": m.rule, "matched_quantity": str(m.qty),
                         "acquired_on": m.acquired_on.isoformat() if m.acquired_on else "pool",
                         "allowable_cost_gbp": str(_r(m.cost_gbp)), "gain_gbp": str(_r(dp.gain_gbp)) if last else ""})
    return rows


def config_hash(policy: Mapping[str, Any]) -> str:
    """What the accountant signs off (P7 exit gate): the exact tax configuration."""
    return content_hash(dict(policy))
