"""Verifier and watchdog (spec §13.6, §7.9).

The verifier recomputes the signal, position risk and NAV through code written separately from the decision
plane: plain Python lists, no numpy, no shared helpers. It works from the spec's definitions (and the
interpretations recorded in engine/signal/trend.py's docstring), not from the engine's functions. A divergence
opens RECON_BREAK and blocks entries. The watchdog blocks entries when the verifier's heartbeat is older than
30 s (VERIFIER_HEARTBEAT_LOST), and lifts the block when heartbeats return.
"""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Context, Decimal, localcontext
from typing import Any

from engine.common.incidents import IncidentLog

BPD = 6


def _cl(x: float) -> float:
    return -1.0 if x < -1.0 else (1.0 if x > 1.0 else x)


def independent_signal(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], i: int,
                       sig: Mapping[str, Any], ewma_lambda: float) -> float | None:
    """T at 4h bar i, recomputed from first principles. None when history is insufficient."""
    brk, mom = list(sig["breakout_lookbacks_d"]), list(sig["momentum_lookbacks_m"])
    if i < max(max(brk), 30 * max(mom)) * BPD:
        return None
    c_now = closes[i]
    b_votes = []
    for days in brk:
        window = closes[i - days * BPD:i]
        hi, lo = max(window), min(window)
        b_votes.append(1.0 if c_now > hi else (-1.0 if c_now < lo else 0.0))
    b = math.fsum(b_votes) / len(b_votes)
    nd = i // BPD
    dh = [max(highs[d * BPD:(d + 1) * BPD]) for d in range(nd)]
    dl = [min(lows[d * BPD:(d + 1) * BPD]) for d in range(nd)]
    dc = [closes[(d + 1) * BPD - 1] for d in range(nd)]
    rets = [math.log(dc[k] / dc[k - 1]) for k in range(1, nd)]
    var = rets[0] * rets[0]
    for r in rets[1:]:
        var = ewma_lambda * var + (1.0 - ewma_lambda) * r * r
    vol = math.sqrt(var)
    m_parts = []
    for months in mom:
        days = 30 * months
        lr = math.log(c_now / closes[i - days * BPD])
        m_parts.append(_cl(lr / (vol * math.sqrt(days)) / sig["clip_t"]) if vol > 0 else 0.0)
    m = math.fsum(m_parts) / len(m_parts)

    def ema_last(xs: list[float], n: int) -> float:
        k = 2.0 / (n + 1)
        e = xs[0]
        for x in xs[1:]:
            e = k * x + (1 - k) * e
        return e
    trs = []
    for d in range(nd):
        pc = dc[d - 1] if d > 0 else dc[0]
        trs.append(max(dh[d] - dl[d], abs(dh[d] - pc), abs(dl[d] - pc)))
    n_atr = int(sig["atr_n"])
    a = math.fsum(trs[-n_atr:]) / n_atr
    z = _cl((ema_last(dc, int(sig["ema_fast"])) - ema_last(dc, int(sig["ema_slow"]))) / a / sig["clip_t"]) if a > 0 else 0.0
    return math.fsum((b, m, z)) / 3.0


def independent_nav(fills: Sequence[Mapping[str, Any]], flows: Sequence[Mapping[str, Any]],
                    prices_usd: Mapping[str, float], quote: str = "USD") -> Decimal:
    """NAV from raw fills and cash flows, not from the ledger."""
    with localcontext(Context(prec=120)):
        return _nav(fills, flows, prices_usd, quote)


def _nav(fills: Sequence[Mapping[str, Any]], flows: Sequence[Mapping[str, Any]], prices_usd: Mapping[str, float],
         quote: str) -> Decimal:
    hold: dict[str, Decimal] = {}
    for f in flows:
        hold[f["ccy"]] = hold.get(f["ccy"], Decimal(0)) + Decimal(str(f["amount"]))
    for f in fills:
        q, px, fee = Decimal(str(f["qty"])), Decimal(str(f["price"])), Decimal(str(f["fee"]))
        s = Decimal(1) if f["side"] == "buy" else Decimal(-1)
        hold[f["base"]] = hold.get(f["base"], Decimal(0)) + s * q
        hold[quote] = hold.get(quote, Decimal(0)) - s * q * px - fee
    return sum((v * Decimal(str(prices_usd[k])) for k, v in hold.items() if v != 0), Decimal(0))


@dataclass
class Verifier:
    incidents: IncidentLog
    signal_tol: float = 1e-9
    nav_tol: Decimal = Decimal("0.01")
    checks: list[dict[str, Any]] = field(default_factory=list)

    def _result(self, what: str, ok: bool, detail: str) -> bool:
        self.checks.append({"what": what, "ok": ok, "detail": detail})
        if not ok:
            self.incidents.open_incident("RECON_BREAK", "S2", "engine", f"{what}: {detail}")
        return ok

    def check_signal(self, engine_T: float | None, independent_T: float | None, where: str) -> bool:
        if engine_T is None or independent_T is None:
            return self._result(f"signal {where}", engine_T is None and independent_T is None,
                                f"engine={engine_T} verifier={independent_T}")
        return self._result(f"signal {where}", abs(engine_T - independent_T) <= self.signal_tol,
                            f"engine={engine_T:.12f} verifier={independent_T:.12f}")

    def check_nav(self, engine_nav: Decimal, independent: Decimal) -> bool:
        return self._result("nav", abs(engine_nav - independent) <= self.nav_tol, f"engine={engine_nav} verifier={independent}")

    def check_risk(self, qty: float, entry: float, stop: float, r_tier: float, nav: float, where: str) -> bool:
        risk = qty * (entry - stop)
        return self._result(f"risk {where}", risk <= r_tier * nav * (1 + 1e-9), f"risk {risk:.2f} vs {r_tier * nav:.2f}")


@dataclass
class Watchdog:
    incidents: IncidentLog
    timeout_s: float = 30.0
    last: dict[str, float] = field(default_factory=dict)

    def beat(self, who: str, now_s: float) -> None:
        self.last[who] = now_s

    def check(self, now_s: float, who: str = "verifier") -> bool:
        seen = self.last.get(who)
        alive = seen is not None and now_s - seen <= self.timeout_s
        if alive:
            self.incidents.resolve("VERIFIER_HEARTBEAT_LOST", "engine", f"{who} heartbeat back")
        elif "VERIFIER_HEARTBEAT_LOST" not in self.incidents.open_codes("engine"):
            self.incidents.open_incident("VERIFIER_HEARTBEAT_LOST", "S2", "engine", f"{who} silent > {self.timeout_s}s")
        return alive
