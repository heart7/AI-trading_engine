"""Strategy A decision cycle over history, with the PAPER fill model (spec §5, §7, §9.7, §13.4).

One code path serves offline replay and PAPER: at each 4h close the engine computes the signal,
evaluates the gate ladder for every instrument, persists a signal_intent for every outcome, and
simulates fills with the §9.7 model. It never sends an order anywhere.

Fill model (§9.7), with the interpretations 4h bars force:
- Entry: post-only limit at the signal close. It fills as maker only if the next bar OPENS at least
  one tick through the limit (the 15-minute ladder deadline sits at the start of the next bar, and the
  open is the only price known inside it). Otherwise IOC at the next open plus half-spread and q75
  slippage, as taker, provided cost_R is still within budget; otherwise no trade.
- Stops: filled at the worse of the stop and the bar open beyond it, minus q75 slippage, taker fee.
- Signal and time-stop exits: next bar open minus half-spread and slippage, taker fee.
- Gap bars are not interpolated: the instrument is DATA_STALE for that bar (series must be gap-free).
"""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import numpy as np

from engine.common.canonical import content_hash
from engine.risk.gates import EntryContext, check_add, cluster_ok, evaluate_entry, evaluate_exit  # noqa: F401
from engine.risk.ladder import LadderInputs, LossLadder
from engine.router.router import StrategyRouter
from engine.signal.trend import SignalParams, signal_series
from engine.sizing.cost import CostInput, cost_gate
from engine.sizing.sizing import Holding, SizingInputs, compute_size
from engine.stops.stops import StopState, TimeStop, open_stop

H4S = 4 * 3600


@dataclass(frozen=True)
class CostModel:
    """ASSUMED venue costs (A-FEES-KRAKEN). Fractions of notional."""
    maker_fee: float = 0.0025
    taker_fee: float = 0.0040
    half_spread: float = 0.0005
    slippage_q75: float = 0.0005
    tick_frac: float = 0.0001

    @property
    def roundtrip_fee(self) -> float:
        return self.maker_fee + self.taker_fee


@dataclass
class Series:
    instrument_id: str
    open_time: np.ndarray  # int seconds, contiguous 4h grid, first bar at 00:00 UTC
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray  # noqa: E741
    c: np.ndarray
    certified: bool = False
    depth_50bp_usd: float = 2.0e6  # ASSUMED liquidity until order-book snapshots exist
    volume_usd_per_min: float = 5.0e5


@dataclass
class Position:
    instrument_id: str
    qty: float
    entry_px: float
    entry_bar: int
    stop: StopState
    risk_usd: float
    fees: float
    adds: int = 0


@dataclass
class Trade:
    instrument_id: str
    entry_time: int
    exit_time: int
    entry_px: float
    exit_px: float
    qty: float
    pnl: float
    R: float
    fees: float
    bars_held: int
    exit_reason: str
    entry_liquidity: str


@dataclass
class ReplayConfig:
    nav0: float = 30_000.0
    mode: str = "PAPER"
    # From the harness: a constant, or a per-instrument per-bar array (walk-forward). None/NaN means size 0 (§5.5).
    mu_q_daily: float | Mapping[str, np.ndarray] | None = None
    sigma_star: float | None = None  # falls back to the policy anchor, flagged ASSUMED
    evidence_on_file: bool = True  # PAPER needs no validation evidence; SHADOW+ do (§9.3)
    engine_running: bool = True
    keep_intents_for_last_cycles: int = 6
    vol_deflation: bool = True
    es_mult: float | None = None  # research override for the ES-limit study; None reads policy
    components: tuple[str, ...] = ("B", "M", "Z")
    kelly_enabled: bool = True  # research plane only (see research/harness/steps.py)


@dataclass
class ReplayResult:
    trades: list[Trade]
    equity_time: np.ndarray
    equity: np.ndarray
    daily_time: np.ndarray
    daily_returns: np.ndarray
    funnel: dict[str, int]
    binding_counts: dict[str, int]
    sizing_binding: dict[str, int]
    intents_tail: list[dict]
    ladder_events: list[tuple[str, str]]
    signals: dict[str, dict[str, np.ndarray]]
    stops: dict[str, list[tuple[int, float]]]
    assumed: list[str]
    result_hash: str = ""  # exact, bit-for-bit within one environment
    fingerprint: str = ""  # decisions + values to 9 significant digits: stable across CPUs and numpy SIMD paths

    def summary(self) -> dict:
        r = self.daily_returns
        sharpe = float(np.mean(r) / np.std(r, ddof=1) * math.sqrt(365)) if len(r) > 2 and np.std(r) > 0 else 0.0
        peak = np.maximum.accumulate(self.equity) if len(self.equity) else np.array([1.0])
        mdd = float(np.max(1 - self.equity / peak)) if len(self.equity) else 0.0
        wins = [t for t in self.trades if t.pnl > 0]
        return {"trades": len(self.trades), "win_rate": len(wins) / len(self.trades) if self.trades else None,
                "avg_R": float(np.mean([t.R for t in self.trades])) if self.trades else None,
                "net_return": float(self.equity[-1] / self.equity[0] - 1) if len(self.equity) else 0.0,
                "sharpe_daily_ann": sharpe, "max_drawdown": mdd, "days": int(len(r)),
                "result_hash": self.result_hash, "class": "REPORTED" if "FIXTURE" in " ".join(self.signals) else "DERIVED"}


def _mu_q(src, k: str, i: int) -> float | None:
    if src is None or isinstance(src, (int, float)):
        return src
    v = float(src[k][i])
    return None if math.isnan(v) else v


def _loss_over(navs: list[float], nav: float, nav0: float, days: int) -> float:
    """Rolling net loss over `days` daily closes, as a positive fraction of NAV."""
    if len(navs) <= days:
        return max(0.0, 1 - nav / nav0) if navs else 0.0
    return max(0.0, 1 - nav / navs[-days - 1])


FUNNEL = ["evaluated", "data_admissible", "breakout", "t_entry", "cost_gate", "risk_caps", "entered"]


def replay(series: Sequence[Series], policy: Mapping, router: StrategyRouter, cfg: ReplayConfig | None = None,
           costs: CostModel | None = None) -> ReplayResult:
    cfg, costs = cfg or ReplayConfig(), costs or CostModel()
    p = SignalParams.from_policy(policy)
    if cfg.components != p.components:
        from dataclasses import replace as _replace
        p = _replace(p, components=tuple(cfg.components))
    es_mult = cfg.es_mult if cfg.es_mult is not None else policy["risk"]["es975_mult"]
    n = len(series[0].c)
    grid = series[0].open_time
    for s in series:
        if len(s.c) != n or not np.array_equal(s.open_time, grid):
            raise ValueError("replay needs aligned, gap-free series")
        if np.any(np.diff(s.open_time) != H4S) or s.open_time[0] % 86400 != 0:
            raise ValueError("series must be contiguous 4h bars starting at 00:00 UTC")
    sig = {s.instrument_id: signal_series(s.h, s.l, s.c, p) for s in series}
    stops_cfg, sz, rk = policy["stops"], policy["sizing"], policy["risk"]
    k_stop, k_trail = stops_cfg["k_stop"], stops_cfg["k_trail"]
    tstop = TimeStop(stops_cfg["time_stop"]["flag_x"], stops_cfg["time_stop"]["reduce_x"], stops_cfg["time_stop"]["default_dwell_bars"])
    sigma_star = cfg.sigma_star if cfg.sigma_star is not None else (sz["sigma_star"]["value"] or sz["sigma_star"]["anchor"])
    assumed = ["A-FEES-KRAKEN", "liquidity depth/volume"]
    if cfg.sigma_star is None and sz["sigma_star"]["value"] is None:
        assumed.append("sigma_star=anchor (step 8 not run)")
    ladder = LossLadder(rk["ladder"])
    sleeve = "A_long"

    cash = cfg.nav0
    positions: dict[str, Position] = {}
    pending_entries: dict[str, dict] = {}
    pending_exits: dict[str, str] = {}
    trades: list[Trade] = []
    eq_t, eq = [], []
    funnel = dict.fromkeys(FUNNEL, 0)
    binding_counts: dict[str, int] = {}
    sizing_binding: dict[str, int] = {}
    intents_tail: list[dict] = []
    stops_hist: dict[str, list[tuple[int, float]]] = {s.instrument_id: [] for s in series}
    by_id = {s.instrument_id: s for s in series}
    day_start_nav = cfg.nav0
    daily_navs: list[tuple[int, float]] = []
    hwm = cfg.nav0
    tail_from = max(0, n - cfg.keep_intents_for_last_cycles)

    def nav_at(i: int) -> float:
        return cash + math.fsum(pos.qty * by_id[k].c[i] for k, pos in positions.items())

    def close_position(k: str, px: float, i: int, reason: str) -> None:
        nonlocal cash
        pos = positions.pop(k)
        fee = pos.qty * px * costs.taker_fee
        cash += pos.qty * px - fee
        pnl = pos.qty * (px - pos.entry_px) - pos.fees - fee
        trades.append(Trade(k, int(grid[pos.entry_bar]), int(grid[i]), pos.entry_px, px, pos.qty, pnl,
                            pnl / pos.risk_usd if pos.risk_usd > 0 else 0.0, pos.fees + fee, i - pos.entry_bar, reason,
                            getattr(pos, "liquidity", "taker")))

    for i in range(n):
        t_open = int(grid[i])
        # 1. fills that happen during bar i: pending exits and entries decided at close i-1, then stops
        for k, reason in list(pending_exits.items()):
            if k in positions:
                s = by_id[k]
                close_position(k, s.o[i] * (1 - costs.half_spread - costs.slippage_q75), i, reason)
        pending_exits.clear()
        for k, e in list(pending_entries.items()):
            s = by_id[k]
            limit = e["limit"]
            if s.o[i] <= limit * (1 - costs.tick_frac):
                px, fee_rate, liq = limit, costs.maker_fee, "maker"
            else:
                px = s.o[i] * (1 + costs.half_spread + costs.slippage_q75)
                fee_rate, liq = costs.taker_fee, "taker"
                d = k_stop * e["atr"] / px
                if d <= 0 or (costs.roundtrip_fee + 2 * costs.half_spread + costs.slippage_q75) / d > policy["cost"]["cost_R_max"]:
                    continue  # IOC would breach the cost budget: no trade
            qty = e["notional"] / px
            st = open_stop(px, e["atr"], k_stop, k_trail, 1)
            risk = qty * (px - st.initial)
            if risk > e["max_risk"] * 1.0000001:
                qty = e["max_risk"] / (px - st.initial)
                risk = e["max_risk"]
            fee = qty * px * fee_rate
            cash -= qty * px + fee
            pos = Position(k, qty, px, i, st, risk, fee)
            pos.liquidity = liq  # type: ignore[attr-defined]
            positions[k] = pos
            funnel["entered"] += 1
        pending_entries.clear()
        for k in list(positions):
            s, pos = by_id[k], positions[k]
            if pos.stop.hit(s.l[i], s.h[i]):
                stop_px = pos.stop.active
                px = min(stop_px, s.o[i]) * (1 - costs.slippage_q75)
                reason = "INITIAL_STOP" if pos.stop.initial >= pos.stop.trail else "TRAILING_STOP"
                close_position(k, px, i, reason)

        # 2. bar i has closed: mark to market, daily loss ladder
        nav = nav_at(i)
        eq_t.append(t_open + H4S)
        eq.append(nav)
        close_ts = t_open + H4S
        now = datetime.fromtimestamp(close_ts, tz=timezone.utc)
        if close_ts % 86400 == 0:
            daily_navs.append((close_ts, nav))
            day_start_nav = nav
        hwm = max(hwm, nav)
        navs = [v for _, v in daily_navs]

        dec = ladder.evaluate(LadderInputs(now, max(0.0, 1 - nav / day_start_nav), _loss_over(navs, nav, cfg.nav0, 5), _loss_over(navs, nav, cfg.nav0, 20),
                                           1 - nav / hwm, daily_review_logged=True))

        # 3. exits for open positions (never evidence-, cost- or budget-gated), then ratchet stops
        for k, pos in list(positions.items()):
            s, g = by_id[k], sig[k]
            a = g["atr_daily"][i]
            if not math.isnan(a):
                pos.stop.on_close(s.c[i], a, k_trail)
            stops_hist[k].append((close_ts, pos.stop.active))
            T = None if math.isnan(g["T"][i]) else float(g["T"][i])
            ex = evaluate_exit(side=1, T=T, stop_hit=False, initial_hit=False, time_stop_state=tstop.state(i - pos.entry_bar),
                               risk_instruction=dec.flatten)
            if ex.exit:
                pending_exits[k] = ex.reason or "EXIT"

        # 4. entries
        held = [k for k in positions if k not in pending_exits]
        r_tier = router.r_tier(sleeve, cfg.mode) * dec.r_multiplier
        n_max = router.n_max(sleeve, cfg.mode)
        open_risk = [max(0.0, positions[k].qty * (positions[k].entry_px - positions[k].stop.active)) for k in held]
        book = [Holding(positions[k].qty * by_id[k].c[i], float(sig[k]["sigma_daily"][i]) if not math.isnan(sig[k]["sigma_daily"][i]) else 0.05)
                for k in held]
        gross = math.fsum(h.notional for h in book)
        for s in series:
            k, g = s.instrument_id, sig[s.instrument_id]
            if k in positions:
                continue
            funnel["evaluated"] += 1
            B = None if math.isnan(g["B"][i]) else float(g["B"][i])
            T = None if math.isnan(g["T"][i]) else float(g["T"][i])
            abstain = "INSUFFICIENT_HISTORY" if T is None else None
            a = g["atr_daily"][i]
            px = float(s.c[i])
            cg = None
            size_usd, max_risk = 0.0, 0.0
            if T is not None:
                funnel["data_admissible"] += 1
                cgr = cost_gate(k_stop=k_stop, atr_daily=float(a), entry_px=px,
                                fee_roundtrip=CostInput(costs.roundtrip_fee, now, timedelta(days=7)),
                                spread_at_size=CostInput(2 * costs.half_spread, now, timedelta(seconds=60)),
                                slippage_q75=CostInput(costs.slippage_q75, now, timedelta(days=1)),
                                cost_R_max=policy["cost"]["cost_R_max"], now=now)
                cg = cgr.gate
                d = cgr.d
                si = SizingInputs(nav=nav, r_tier=r_tier, d=d, sigma_daily=float(g["sigma_daily"][i]),
                                  mu_q_daily=_mu_q(cfg.mu_q_daily, k, i), kelly_k=sz["kelly_k"], es_mult=es_mult,
                                  rho_stress=sz["cluster"]["rho_stress"], depth_50bp_usd=s.depth_50bp_usd,
                                  volume_usd_per_min=s.volume_usd_per_min, ttf_ceiling_min=rk["ttf_ceiling_min"]["A"],
                                  per_pair_notional_max=sz["per_pair_notional_max"],
                                  per_instrument_risk_share_max=sz["per_instrument_risk_share_max"],
                                  open_risk_cap=sz["cluster"]["open_risk_cap"], sigma_star_annual=sigma_star,
                                  regime_authority=policy["regime"]["authority"], book=book, gross_now=gross,
                                  vol_deflation=cfg.vol_deflation, kelly_enabled=cfg.kelly_enabled)
                res = compute_size(si)
                if B is not None and B > 0 and T >= p.T_entry:
                    key = res.zero_reason or res.binding_limit
                    sizing_binding[key] = sizing_binding.get(key, 0) + 1
                venue_room = max(0.0, (policy["risk"]["venue_exposure_max"] - policy["collateral"]["spot_operating_buffer"]) * nav - gross)
                size_usd = min(res.size_usd * dec.size_multiplier, venue_room)
                max_risk = size_usd * d
            risk_ok = (size_usd > 0 and len(held) < n_max and cluster_ok(open_risk, max_risk, sz["cluster"]["open_risk_cap"], nav)
                       and dec.entries_allowed)
            ladder_rows, binding = evaluate_entry(EntryContext(
                engine_running=cfg.engine_running and not dec.reduce_only, sleeve_allowed=router.sleeve_allowed(sleeve, cfg.mode),
                data_fresh=True, data_certified=s.certified or cfg.mode == "PAPER", signal_abstain=abstain, admissible=T is not None,
                B=B, T=T, T_entry=p.T_entry, side=1, evidence_on_file=cfg.evidence_on_file, cost_gate=cg,
                cost_R=None, cost_R_max=policy["cost"]["cost_R_max"], risk_budget_ok=risk_ok))
            if B is not None and B > 0 and T is not None:
                funnel["breakout"] += 1
                if T >= p.T_entry:
                    funnel["t_entry"] += 1
                    if cg is None:
                        funnel["cost_gate"] += 1
                        if risk_ok:
                            funnel["risk_caps"] += 1
            binding_counts[binding or "ENTER"] = binding_counts.get(binding or "ENTER", 0) + 1
            outcome = "ENTERED" if binding is None else ("ABSTAINED" if abstain else "REJECTED")
            if i >= tail_from:
                intents_tail.append({"instrument_id": k, "bar_close": now.isoformat(), "sleeve": sleeve, "outcome": outcome,
                                     "binding_gate": binding, "gate_ladder": ladder_rows})
            if binding is None and i + 1 < n:
                pending_entries[k] = {"limit": px, "atr": float(a), "notional": size_usd, "max_risk": r_tier * nav}
                held.append(k)
                open_risk.append(max_risk)
                gross += size_usd
                book.append(Holding(size_usd, float(g["sigma_daily"][i])))

    # the last bar's decisions have no next bar to fill in
    funnel["entered"] = math.fsum(1 for _ in trades) + len(positions)
    for k in list(positions):
        close_position(k, float(by_id[k].c[-1]), n - 1, "END_OF_REPLAY")
    dt = np.array([t for t, _ in daily_navs], dtype=np.int64)
    dn = np.array([v for _, v in daily_navs])
    dr = dn[1:] / dn[:-1] - 1 if len(dn) > 1 else np.array([])
    res = ReplayResult(trades, np.array(eq_t), np.array(eq), dt[1:], dr, funnel, binding_counts, sizing_binding, intents_tail,
                       [(t.isoformat(), r) for t, r in ladder.state.events], sig, stops_hist, assumed)
    res.result_hash = content_hash({
        "trades": [[t.instrument_id, t.entry_time, t.exit_time, repr(t.entry_px), repr(t.exit_px), repr(t.qty), t.exit_reason] for t in trades],
        "equity": [repr(float(x)) for x in eq[::6]], "funnel": funnel})
    g = lambda x: f"{float(x):.9g}"  # noqa: E731
    res.fingerprint = content_hash({
        "trades": [[t.instrument_id, t.entry_time, t.exit_time, g(t.entry_px), g(t.exit_px), g(t.qty), t.exit_reason] for t in trades],
        "equity": [g(x) for x in eq[::6]], "funnel": funnel})
    return res
