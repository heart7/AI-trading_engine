"""PAPER replay for the Strategy B books (spec §6, P4). B never trades live below T3 and runs PAPER from T0 to
supply evidence. One book per run: B_short (side -1) or B_long (side +1).

Timing follows the Strategy A replay: decide at the 4h close, fill at the next open. Entries pass the perp cost
gate with *conditional* funding estimated walk-forward from events already seen (zero-or-adverse until enough
exist); sizes come from §6.6 with isolated margin and the simultaneous-liquidation invariant. Funding accrues at
each contract's own interval from its spec; `funding` holds (event_ts, rate) arrays per instrument. Exits: T crosses 0 against the position, initial or trailing stop,
funding-aware time stop (reduce at 0.5R, taken as a full exit here), loss-ladder flatten.
"""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace

import numpy as np

from engine.common.canonical import content_hash
from engine.replay.paper import CostModel, ReplayResult, Series, Trade
from engine.signal.trend import SignalParams, signal_series
from engine.stops.stops import open_stop
from engine.strategy_b.contracts import ContractSpec
from engine.strategy_b.funding import OutcomeWeightedHold, conditional_estimate, funding_time_stop, perp_cost_gate
from engine.strategy_b.margin import MarginInvariant, PerpOrder, PerpPosition, check_order, size_b

H4S = 4 * 3600
PERP_COSTS = CostModel(maker_fee=0.0002, taker_fee=0.0005, half_spread=0.0003, slippage_q75=0.0005)  # A-FEES-PERPS


@dataclass
class PerpConfig:
    book: str = "B_short"
    nav0: float = 30_000.0
    components: tuple[str, ...] = ("B", "M", "Z")
    vol_deflation: bool = True
    sigma_star: float | None = None
    tier_for_params: str = "T4"  # PAPER runs with the book's own declared parameters
    min_hold_prior_days: tuple[float, float, float] = (0.4, 20.0, 8.0)  # ASSUMED h_ow prior until episodes exist
    kelly_enabled: bool = False  # accepted for harness compatibility; B sizing has no Kelly term
    es_mult: float | None = None
    extra: dict = field(default_factory=dict)


@dataclass
class _Pos:
    k: str
    side: int
    qty: float
    entry_px: float
    entry_bar: int
    stop: object
    risk_usd: float
    fees: float
    leverage: float
    funding_paid: float = 0.0


def replay_perp(series: Sequence[Series], policy: Mapping, specs: Mapping[str, ContractSpec],
                funding: Mapping[str, tuple[np.ndarray, np.ndarray]], cfg: PerpConfig | None = None,
                costs: CostModel | None = None) -> ReplayResult:
    cfg = cfg or PerpConfig()
    base_costs = costs or PERP_COSTS
    side = -1 if cfg.book == "B_short" else 1
    p = replace(SignalParams.from_policy(policy), components=tuple(cfg.components))
    tier = policy["tiers"][cfg.tier_for_params]
    r_b = float(tier["r"]["B"])
    n_max = int(sum(v for k, v in tier["n_max"].items() if k.startswith("B")))
    k_stop, k_trail = policy["stops"]["k_stop"], policy["stops"]["k_trail"]
    fts = policy["perps"]["funding_time_stop_R"]
    kc, lam = policy["cost"]["perps"]["k_cost"], policy["cost"]["perps"]["lambda_carry"]
    sigma_star = cfg.sigma_star or policy["sizing"]["sigma_star"]["value"] or policy["sizing"]["sigma_star"]["anchor"]
    grid = series[0].open_time
    n = len(grid)
    sig = {s.instrument_id: signal_series(s.h, s.l, s.c, p) for s in series}
    by = {s.instrument_id: s for s in series}
    cash = cfg.nav0
    pos: dict[str, _Pos] = {}
    pend_in: dict[str, dict] = {}
    pend_out: dict[str, str] = {}
    trades: list[Trade] = []
    eq_t, eq = [], []
    funnel = {"evaluated": 0, "data_admissible": 0, "breakout": 0, "t_entry": 0, "cost_gate": 0, "risk_caps": 0, "entered": 0}
    sizing_binding: dict[str, int] = {}
    ep_pnl: list[float] = []
    ep_hold: list[float] = []
    hwm = cfg.nav0
    daily_navs: list[float] = []

    def nav_at(i: int) -> float:
        return cash + math.fsum(q.side * q.qty * (by[k].c[i] - q.entry_px) for k, q in pos.items())

    def close(k: str, px: float, i: int, reason: str) -> None:
        nonlocal cash
        q = pos.pop(k)
        fee = q.qty * px * base_costs.taker_fee
        pnl = q.side * q.qty * (px - q.entry_px) - q.fees - fee - q.funding_paid
        cash += q.side * q.qty * (px - q.entry_px) - fee
        trades.append(Trade(k, int(grid[q.entry_bar]), int(grid[i]), q.entry_px, px, q.qty, pnl,
                            pnl / q.risk_usd if q.risk_usd > 0 else 0.0, q.fees + fee + q.funding_paid, i - q.entry_bar,
                            reason, "taker"))
        ep_pnl.append(pnl)
        ep_hold.append((i - q.entry_bar) / 6)

    for i in range(n):
        close_ts = int(grid[i]) + H4S
        for k, why in list(pend_out.items()):
            if k in pos:
                close(k, by[k].o[i] * (1 - side * (base_costs.half_spread + base_costs.slippage_q75)), i, why)
        pend_out.clear()
        for k, e in list(pend_in.items()):
            s = by[k]
            px = s.o[i] * (1 + side * (base_costs.half_spread + base_costs.slippage_q75))
            st = open_stop(px, e["atr"], k_stop, k_trail, side)
            qty = e["notional_usd"] / px
            risk = qty * abs(px - st.initial)
            fee = qty * px * base_costs.taker_fee
            cash -= fee
            pos[k] = _Pos(k, side, qty, px, i, st, risk, fee, e["leverage"])
            funnel["entered"] += 1
        pend_in.clear()
        for k in list(pos):
            s, q = by[k], pos[k]
            if q.stop.hit(s.l[i], s.h[i]):
                trig = q.stop.active
                px = min(trig, s.o[i]) * (1 - base_costs.slippage_q75) if side > 0 else \
                    max(trig, s.o[i]) * (1 + base_costs.slippage_q75)
                close(k, px, i, "INITIAL_STOP" if q.stop.active == q.stop.initial else "TRAILING_STOP")
        # funding events inside this bar, at each contract's own interval (from its spec, never assumed)
        for k, q in pos.items():
            ts_k, rt_k = funding[k]
            lo, hi = np.searchsorted(ts_k, close_ts - H4S, side="right"), np.searchsorted(ts_k, close_ts, side="right")
            if hi > lo:
                paid = q.side * float(rt_k[lo:hi].sum()) * q.qty * by[k].c[i]
                q.funding_paid += paid
                cash -= paid
        nav = nav_at(i)
        eq_t.append(close_ts)
        eq.append(nav)
        hwm = max(hwm, nav)
        if close_ts % 86400 == 0:
            daily_navs.append(nav)
        flatten = 1 - nav / hwm >= policy["risk"]["ladder"]["dd"]["flatten_decision"]
        for k, q in list(pos.items()):
            g = sig[k]
            a = g["atr_daily"][i]
            if not math.isnan(a):
                q.stop.on_close(by[k].c[i], a, k_trail)
            T = g["T"][i]
            why = None
            if flatten:
                why = "FLATTEN"
            elif not math.isnan(T) and side * T <= 0:
                why = "T_CROSS_ZERO"
            elif funding_time_stop(q.funding_paid / q.risk_usd if q.risk_usd else 0.0, fts["flag"], fts["reduce"]) == "REDUCE":
                why = "FUNDING_TIME_STOP"
            if why:
                pend_out[k] = why
        book = [PerpPosition(k, q.side, q.qty * by[k].c[i] / nav, q.leverage) for k, q in pos.items() if k not in pend_out]
        realised = np.diff(np.log(np.asarray(daily_navs[-60:]))) if len(daily_navs) > 20 else np.array([])
        v = min(1.0, sigma_star / (realised.std() * math.sqrt(365))) if cfg.vol_deflation and len(realised) and realised.std() > 0 else 1.0
        for s in series:
            k, g = s.instrument_id, sig[s.instrument_id]
            if k in pos or flatten:
                continue
            funnel["evaluated"] += 1
            T, B, a = g["T"][i], g["B"][i], g["atr_daily"][i]
            if math.isnan(T) or math.isnan(a):
                continue
            funnel["data_admissible"] += 1
            if not side * B > 0:
                continue
            funnel["breakout"] += 1
            if side * T < p.T_entry:
                continue
            funnel["t_entry"] += 1
            px = float(s.c[i])
            d = k_stop * float(a) / px
            ts_k, rt_k = funding[k]
            seen = np.searchsorted(ts_k, close_ts, side="right")
            ev_bar = np.minimum((ts_k[:seen] - int(grid[0]) - 1) // H4S, i).astype(int)
            state = side * np.nan_to_num(sig[k]["T"][ev_bar]) >= p.T_entry
            est = conditional_estimate(k, cfg.book, rt_k[:seen], state, specs[k])
            hold = OutcomeWeightedHold.from_episodes(ep_pnl, ep_hold) if len(ep_pnl) >= 10 else \
                OutcomeWeightedHold(*cfg.min_hold_prior_days)
            certain = 2 * base_costs.taker_fee + 2 * base_costs.half_spread + base_costs.slippage_q75
            gate = perp_cost_gate(e_gross=2 * d, certain_cost=certain, funding=est, hold=hold, k_cost=kc, lam=lam)
            if not gate["pass"]:
                continue
            funnel["cost_gate"] += 1
            sz = size_b(policy, r_b=r_b, stop_distance=d, mmr=specs[k].mmr(nav * r_b / d), book=book, v=v)
            sizing_binding[sz["binding"]] = sizing_binding.get(sz["binding"], 0) + 1
            if len(book) + len(pend_in) >= n_max or sz["notional"] <= 0:
                continue
            order = PerpOrder(k, side, sz["notional"], sz["leverage"])
            try:
                check_order(policy, book + [PerpPosition(x, side, e["notional_usd"] / nav, e["leverage"])
                                            for x, e in pend_in.items()], order)
            except MarginInvariant:
                continue
            funnel["risk_caps"] += 1
            pend_in[k] = {"notional_usd": sz["notional"] * nav, "leverage": sz["leverage"], "atr": float(a)}

    eq_arr, t_arr = np.asarray(eq), np.asarray(eq_t)
    day_mask = t_arr % 86400 == 0
    d_eq, d_t = eq_arr[day_mask], t_arr[day_mask]
    d_ret = np.diff(d_eq) / d_eq[:-1] if len(d_eq) > 1 else np.array([])
    g9 = lambda x: f"{float(x):.9g}"  # noqa: E731
    fp = content_hash({"trades": [[t.instrument_id, t.entry_time, t.exit_time, g9(t.entry_px), g9(t.exit_px), t.exit_reason]
                                  for t in trades], "funnel": funnel, "book": cfg.book})
    return ReplayResult(trades, t_arr, eq_arr, d_t[1:], d_ret, funnel, {}, sizing_binding, [], [], sig, {},
                        ["A-FEES-PERPS", "FIXTURE funding", "h_ow prior"], fp, fp)
