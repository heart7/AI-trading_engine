"""Double-entry, append-only, hash-chained ledger (spec §12.1).

Amounts are `Decimal` so balances and attribution sum to the penny. Every transaction balances to zero
per currency. Each entry carries the hash of the one before it; `verify()` walks the chain and any break
is an incident (LEDGER_CHAIN_BREAK). Deposits and withdrawals post to capital accounts, never to P&L (INV-41).

Account names: cash:<venue>:<ccy>, asset:<venue>:<ccy> (coins held), capital:<ccy>, pnl:trading:<ccy>,
expense:fees:<ccy>, reserve:tax:<ccy>, offvenue:<where>:<ccy>, transfer:clearing:<ccy>.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Context, Decimal, localcontext

from engine.common.canonical import content_hash
from engine.common.incidents import IncidentLog

GENESIS = "0" * 64
# Enough digits that products of float-derived inputs are exact: "to the penny" then holds with no rounding at all.
EXACT = Context(prec=120)
D0 = Decimal(0)


def dec(x: float | int | str | Decimal) -> Decimal:
    return x if isinstance(x, Decimal) else Decimal(str(x))


class LedgerUnbalanced(Exception):
    pass


@dataclass(frozen=True)
class Entry:
    seq: int
    tx_id: str
    account: str
    amount: Decimal  # debit positive, credit negative
    currency: str
    ref: str  # source fill / funding / transfer record
    kind: str  # FILL | FEE | DEPOSIT | WITHDRAWAL | TRANSFER | TAX_RESERVE | DUST
    prev_hash: str
    hash: str


def _hash(seq: int, tx_id: str, account: str, amount: Decimal, currency: str, ref: str, kind: str, prev: str) -> str:
    return content_hash({"seq": seq, "tx": tx_id, "acct": account, "amt": str(amount), "ccy": currency, "ref": ref,
                         "kind": kind, "prev": prev})


@dataclass
class Ledger:
    entries: list[Entry] = field(default_factory=list)
    _refs: set[str] = field(default_factory=set)

    @property
    def head(self) -> str:
        return self.entries[-1].hash if self.entries else GENESIS

    def post(self, tx_id: str, legs: Iterable[tuple[str, Decimal | float, str]], *, ref: str, kind: str) -> list[Entry]:
        """Post one balanced transaction. Idempotent per `ref`: the same source record never posts twice."""
        if ref in self._refs:
            return []
        legs = [(a, dec(x), c) for a, x, c in legs]
        per_ccy: dict[str, Decimal] = defaultdict(lambda: D0)
        with localcontext(EXACT):
            for _, amt, ccy in legs:
                per_ccy[ccy] += amt
        bad = {c: s for c, s in per_ccy.items() if s != 0}
        if bad:
            raise LedgerUnbalanced(f"{tx_id} does not balance: {bad}")
        out = []
        for acct, amt, ccy in legs:
            seq, prev = len(self.entries), self.head
            e = Entry(seq, tx_id, acct, amt, ccy, ref, kind, prev, _hash(seq, tx_id, acct, amt, ccy, ref, kind, prev))
            self.entries.append(e)
            out.append(e)
        self._refs.add(ref)
        return out

    def verify(self, incidents: IncidentLog | None = None) -> bool:
        prev = GENESIS
        for i, e in enumerate(self.entries):
            ok = e.seq == i and e.prev_hash == prev and \
                e.hash == _hash(e.seq, e.tx_id, e.account, e.amount, e.currency, e.ref, e.kind, e.prev_hash)
            if not ok:
                if incidents is not None:
                    incidents.open_incident("LEDGER_CHAIN_BREAK", "S1", "engine", f"entry {i}")
                return False
            prev = e.hash
        return True

    def balances(self, prefix: str = "") -> dict[tuple[str, str], Decimal]:
        out: dict[tuple[str, str], Decimal] = defaultdict(lambda: D0)
        with localcontext(EXACT):
            for e in self.entries:
                if e.account.startswith(prefix):
                    out[(e.account, e.currency)] += e.amount
        return {k: v for k, v in out.items() if v != 0}

    def kinds(self) -> set[str]:
        return {e.kind for e in self.entries}

    # -- posting helpers -------------------------------------------------------------------------
    def post_fill(self, *, venue: str, fill_ref: str, side: str, base: str, quote: str, qty: float, price: float,
                  fee: float) -> list[Entry]:
        """Spot fill. Coins and cash swap at the fill price through a trading clearing account per currency."""
        with localcontext(EXACT):
            q, n, f = dec(qty), dec(qty) * dec(price), dec(fee)
        s = 1 if side == "buy" else -1
        return self.post(f"fill:{fill_ref}", [
            (f"asset:{venue}:{base}", s * q, base), ("pnl:trading:" + base, -s * q, base),
            (f"cash:{venue}:{quote}", -s * n, quote), ("pnl:trading:" + quote, s * n, quote),
            (f"cash:{venue}:{quote}", -f, quote), (f"expense:fees:{quote}", f, quote),
        ], ref=f"fill:{fill_ref}", kind="FILL")

    def post_deposit(self, *, venue_or_bank: str, ccy: str, amount: float, ref: str, account: str = "cash") -> list[Entry]:
        a = dec(amount)
        return self.post(f"dep:{ref}", [(f"{account}:{venue_or_bank}:{ccy}", a, ccy), (f"capital:{ccy}", -a, ccy)],
                         ref=f"dep:{ref}", kind="DEPOSIT")

    def post_withdrawal(self, *, venue_or_bank: str, ccy: str, amount: float, ref: str, account: str = "cash") -> list[Entry]:
        a = dec(amount)
        return self.post(f"wd:{ref}", [(f"{account}:{venue_or_bank}:{ccy}", -a, ccy), (f"capital:{ccy}", a, ccy)],
                         ref=f"wd:{ref}", kind="WITHDRAWAL")

    def post_transfer(self, *, frm: str, to: str, ccy: str, amount: float, ref: str) -> list[Entry]:
        """Human-executed sweep between the fund's own accounts (venue -> bank/self-custody). Not P&L."""
        a = dec(amount)
        return self.post(f"xfer:{ref}", [(frm, -a, ccy), (to, a, ccy)], ref=f"xfer:{ref}", kind="TRANSFER")

    def post_tax_reserve(self, *, ccy: str, amount: float, ref: str) -> list[Entry]:
        a = dec(amount)
        return self.post(f"tax:{ref}", [("reserve:tax:" + ccy, a, ccy), ("pnl:tax_reserve:" + ccy, -a, ccy)],
                         ref=f"tax:{ref}", kind="TAX_RESERVE")
