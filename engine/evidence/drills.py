"""P3.5 exit-gate drills (spec §19): "kill verifier -> entries block" and "attribution sums to the penny on replay".

Both return a record that a run record can wrap; `tools/uchfe.py drill` runs them.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

import numpy as np

from engine.common.incidents import IncidentLog
from engine.evidence.attribution import Episode, attribute
from engine.evidence.ledger import Ledger
from engine.evidence.nav import ledger_holdings, value
from engine.evidence.verifier import Verifier, Watchdog, independent_nav
from engine.execution.conformance import INST, SPEC, _sim
from engine.execution.model import OrderRequest, OrderType, Purpose
from engine.execution.oms import OMS, OrderRefused


def kill_verifier_drill() -> dict[str, Any]:
    inc = IncidentLog()
    v = _sim()
    oms = OMS(v, instruments={INST: SPEC}, incidents=inc)
    oms.reconcile()
    wd = Watchdog(inc)
    steps: list[dict[str, Any]] = []

    def try_entry(t: float) -> str:
        wd.check(t)
        try:
            oms.submit(OrderRequest(oms.new_client_id("d"), INST, "buy", OrderType.POST_ONLY, 0.001, Purpose.ENTRY,
                                    price=59_000.0))
            return "SENT"
        except OrderRefused as e:
            return e.code

    wd.beat("verifier", 10)
    steps.append({"t": 10, "verifier": "alive", "entry": try_entry(10)})
    # verifier killed at t=10: no more heartbeats
    steps.append({"t": 39, "verifier": "killed", "entry": try_entry(39)})
    steps.append({"t": 41, "verifier": "killed", "entry": try_entry(41)})
    exit_ok = True
    try:  # protective orders still flow while entries are blocked
        oms.submit(OrderRequest(oms.new_client_id("x"), INST, "sell", OrderType.STOP, 0.0001, Purpose.PROTECT,
                                stop_price=50_000.0, reduce_only=True))
    except OrderRefused:
        exit_ok = False
    except Exception:  # noqa: BLE001 - venue may refuse a stop with no balance; the gate is what is tested
        exit_ok = True
    wd.beat("verifier", 60)
    steps.append({"t": 61, "verifier": "restarted", "entry": try_entry(61)})
    passed = [s["entry"] for s in steps] == ["SENT", "SENT", "VERIFIER_HEARTBEAT_LOST", "SENT"] and exit_ok
    return {"drill": "KILL_VERIFIER", "passed": passed, "steps": steps, "timeout_s": wd.timeout_s}


def replay_evidence(trades: Sequence[Any], series: Mapping[str, Any], *, nav0: float, venue: str = "paper") -> dict[str, Any]:
    """Posts a replay's trades to the ledger, attributes them, and checks both views against the ledger to the penny."""
    led, inc = Ledger(), IncidentLog()
    led.post_deposit(venue_or_bank=venue, ccy="USD", amount=nav0, ref="seed")
    episodes, fills, prices = [], [], {"USD": 1.0}
    mkt = next((s for k, s in series.items() if "BTC" in k), None)  # market factor: BTC (§12.2 BETA)
    mkt_idx = {int(x): k for k, x in enumerate(mkt.open_time)} if mkt is not None else {}
    for n, t in enumerate(trades):
        base = t.instrument_id.split("-")[0]
        s = series[t.instrument_id]
        idx = {int(x): k for k, x in enumerate(s.open_time)}
        c_in, c_out = float(s.c[idx[t.entry_time]]), float(s.c[idx[t.exit_time]])
        led.post_fill(venue=venue, fill_ref=f"{n}-in", side="buy", base=base, quote="USD", qty=t.qty, price=t.entry_px, fee=0)
        led.post_fill(venue=venue, fill_ref=f"{n}-out", side="sell", base=base, quote="USD", qty=t.qty, price=t.exit_px,
                      fee=t.fees)
        fills += [{"base": base, "side": "buy", "qty": t.qty, "price": t.entry_px, "fee": 0},
                  {"base": base, "side": "sell", "qty": t.qty, "price": t.exit_px, "fee": t.fees}]
        prices[base] = c_out
        episodes.append(Episode.of(episode_id=str(n), sleeve="A_long", qty=t.qty, entry_decision=c_in,
                                   entry_model=t.entry_px, entry_fill=t.entry_px, exit_decision=c_out,
                                   exit_model=t.exit_px, exit_fill=t.exit_px, fees=t.fees,
                                   factor_return=(float(mkt.c[mkt_idx[t.exit_time]]) / float(mkt.c[mkt_idx[t.entry_time]]) - 1)
                                   if mkt is not None else c_out / c_in - 1))
    ledger_nav = value(ledger_holdings(led), prices)
    ledger_net = ledger_nav - Decimal(str(nav0))
    att = attribute(episodes, ledger_net=ledger_net, incidents=inc)
    ver = Verifier(inc)
    nav_ok = ver.check_nav(ledger_nav, independent_nav(fills, [{"ccy": "USD", "amount": nav0}], prices))
    float_net = float(np.sum([t.pnl for t in trades])) if trades else 0.0
    return {"drill": "ATTRIBUTION_REPLAY", "passed": att.exact and nav_ok and led.verify(),
            "trades": len(trades), "ledger_net": str(ledger_net.quantize(Decimal("0.01"))),
            "attribution_net": str(att.net.quantize(Decimal("0.01"))),
            "components": {k: str(v.quantize(Decimal("0.01"))) for k, v in att.by_component.items()},
            "replay_float_pnl": round(float_net, 2), "ledger_entries": len(led.entries)}
