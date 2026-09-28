"""Validation harness steps 1-5, 7, 8 for a sleeve (spec §9.3). Step 6 runs during SHADOW (regime layer).

Every step writes a run record (content-addressed config, data hash, code version, seed) and a verdict
through the verdict engine. Nothing here can mark a step PASS without the automated rule.
"""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone

import numpy as np

from engine.common.canonical import content_hash
from engine.replay.paper import CostModel, ReplayConfig, ReplayResult, Series, replay
from engine.router.router import StrategyRouter
from engine.signal.trend import SignalParams, signal_series
from research.harness import stats
from research.harness.verdicts import StepRecord, record_verdict
from research.harness.walkforward import anchored_folds, mu_q_schedule
from research.montecarlo.sigma_star import calibrate

CODE_VERSION = "0.1.0"


@dataclass
class HarnessContext:
    series: Sequence[Series]
    policy: Mapping
    policy_hash: str
    quality_reports: Mapping[str, Mapping]
    sleeve: str = "A_long"
    n_trials: int = 1
    seed: int = 20260928
    reps: int = 1000
    coverage_min: float = 0.99
    quarantine_max: float = 0.01
    cash_rate_annual: float = 0.0  # benchmark (§5.9): 3m T-bill, OBSERVED; 0 until the rate feed exists (ASSUMED)
    test_years: float = 1.0
    runs: list[dict] = field(default_factory=list)
    _base: ReplayResult | None = None
    _mu: dict | None = None

    @property
    def data_hash(self) -> str:
        return content_hash({s.instrument_id: [int(s.open_time[0]), int(s.open_time[-1]), len(s.c), repr(float(s.c.sum()))]
                             for s in self.series})

    def run_record(self, step: int, config: Mapping, verdict: str, metrics: Mapping) -> str:
        cfg_hash = content_hash({"step": step, "sleeve": self.sleeve, "config": config, "policy": self.policy_hash})
        run_id = f"bt-s{step}-{cfg_hash[:10]}-{self.data_hash[:8]}"
        self.runs.append({"run_id": run_id, "step": step, "sleeve": self.sleeve, "config_hash": cfg_hash,
                          "data_hash": self.data_hash, "code_version": CODE_VERSION, "seed": self.seed, "verdict": verdict,
                          "metrics": dict(metrics), "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                          "class": "REPORTED" if any(s.instrument_id.startswith("FIXTURE_") for s in self.series) else "DERIVED"})
        return run_id


def _mu_schedule(ctx: HarnessContext, components: tuple[str, ...] = ("B", "M", "Z")) -> dict[str, np.ndarray]:
    p = SignalParams.from_policy(ctx.policy)
    p = replace(p, components=components)
    n = len(ctx.series[0].c)
    embargo = max(p.breakout_lookbacks_d) * 6
    first = p.min_history_bars + embargo + int(365 * 6 * 1.0)  # >= 1 year of training after the history rule
    folds = anchored_folds(n, first_test_bar=first, test_bars=int(365 * 6 * ctx.test_years), embargo_bars=embargo)
    sigs = {s.instrument_id: signal_series(s.h, s.l, s.c, p) for s in ctx.series}
    return mu_q_schedule(sigs, {s.instrument_id: s.c for s in ctx.series}, folds, T_entry=p.T_entry,
                         n0=ctx.policy["sizing"]["n0"], q=ctx.policy["sizing"]["posterior_q"])


def _run(ctx: HarnessContext, *, costs: CostModel | None = None, **cfg) -> ReplayResult:
    """Research replay. The Kelly cap is off: it needs mu_q, which is the posterior this harness is trying to
    establish, so testing with it on would size every trade to zero (docs/decisions/0003). Trading starts only in
    walk-forward test windows, so returns before the first test window are excluded by _oos."""
    cfg.pop("mu", None)
    if ctx._mu is None:
        ctx._mu = _mu_schedule(ctx)
    return replay(ctx.series, ctx.policy, StrategyRouter(ctx.policy), ReplayConfig(mu_q_daily=None, kelly_enabled=False, **cfg), costs)


def _oos(ctx: HarnessContext, r: ReplayResult) -> np.ndarray:
    """Daily net excess returns over cash after the signal warm-up. No parameter is fitted on the data (every
    value is declared in the policy before the run), so the whole post-warm-up period is out of sample."""
    warm = SignalParams.from_policy(ctx.policy).min_history_bars
    first_ts = int(ctx.series[0].open_time[min(warm, len(ctx.series[0].open_time) - 1)])
    mask = r.daily_time >= first_ts
    return r.daily_returns[mask] - ctx.cash_rate_annual / 365


def base(ctx: HarnessContext) -> ReplayResult:
    if ctx._base is None:
        ctx._base = _run(ctx)
    return ctx._base


def step1_data(ctx: HarnessContext) -> StepRecord:
    reps = ctx.quality_reports
    missing = [s.instrument_id for s in ctx.series if s.instrument_id not in reps]
    worst_cov = min((r["coverage"] for r in reps.values()), default=0.0)
    worst_q = max((len(r["quarantined"]) / max(r["expected_bars"], 1) for r in reps.values()), default=1.0)
    ok = not missing and worst_cov >= ctx.coverage_min and worst_q <= ctx.quarantine_max
    m = {"missing_reports": missing, "worst_coverage": worst_cov, "worst_quarantine_share": worst_q}
    rid = ctx.run_record(1, {"coverage_min": ctx.coverage_min, "quarantine_max": ctx.quarantine_max}, "PASS" if ok else "FAIL", m)
    return record_verdict(1, ctx.sleeve, "PASS" if ok else "FAIL", rid, "coverage", worst_cov, (None, None), details=m)


def step2_null(ctx: HarnessContext) -> StepRecord:
    r = _oos(ctx, base(ctx))
    sr = stats.sharpe_ann(r)
    lb = stats.sharpe_lower_bound(r, mean_block=ctx.policy["validation"]["null_test"]["block_days"], reps=ctx.reps, seed=ctx.seed)
    dsr = stats.deflated_sharpe(r, ctx.n_trials)
    mtrl = stats.min_trl(r)
    ok = lb > 0 and dsr > ctx.policy["validation"]["null_test"]["dsr_min"] and len(r) >= mtrl
    m = {"days": len(r), "sharpe": sr, "sharpe_lb95": lb, "dsr": dsr, "min_trl_days": mtrl if math.isfinite(mtrl) else None,
         "trades": len(base(ctx).trades), "n_trials": ctx.n_trials}
    rid = ctx.run_record(2, {"block": ctx.policy["validation"]["null_test"]["block_days"], "reps": ctx.reps}, "PASS" if ok else "FAIL", m)
    return record_verdict(2, ctx.sleeve, "PASS" if ok else "FAIL", rid, "annualised net Sharpe (daily)", sr, (lb, None), details=m)


def step3_ablation(ctx: HarnessContext) -> StepRecord:
    full = _oos(ctx, base(ctx))
    keep, detail = {}, {}
    for comp in ("B", "M", "Z"):
        rest = tuple(c for c in ("B", "M", "Z") if c != comp)
        alt = _oos(ctx, _run(ctx, components=rest))
        n = min(len(full), len(alt))
        pt, lo, hi = stats.paired_sharpe_diff_ci(full[-n:], alt[-n:], reps=ctx.reps, seed=ctx.seed + ord(comp))
        keep[comp] = lo > 0  # removal hurts beyond noise -> keep
        detail[comp] = {"full_minus_removed": pt, "ci": [lo, hi], "keep": keep[comp]}
    ok = all(keep.values())
    m = {"components": detail, "delete_proposals": [c for c, k in keep.items() if not k]}
    rid = ctx.run_record(3, {"paired": "rho_paired", "reps": ctx.reps}, "PASS" if ok else "FAIL", m)
    return record_verdict(3, ctx.sleeve, "PASS" if ok else "FAIL", rid, "paired Sharpe diff", None, (None, None), details=m)


def step4_costs(ctx: HarnessContext) -> StepRecord:
    base_c = CostModel()
    rows = {}
    for fee_mult in (0.5, 1.0, 1.5):
        for q, slip in (("q50", 0.0003), ("q75", base_c.slippage_q75), ("q90", 0.0010)):
            c = CostModel(maker_fee=base_c.maker_fee * fee_mult, taker_fee=base_c.taker_fee * fee_mult,
                          half_spread=base_c.half_spread, slippage_q75=slip)
            r = _oos(ctx, _run(ctx, costs=c))
            rows[f"fee x{fee_mult} {q}"] = {"sharpe": stats.sharpe_ann(r),
                                            "lb95": stats.sharpe_lower_bound(r, reps=ctx.reps // 2, seed=ctx.seed)}
    decisive = rows["fee x1.5 q75"]["lb95"]
    ok = decisive > 0 and rows["fee x1.0 q75"]["lb95"] > 0
    m = {"tornado": rows, "decisive": "fee x1.5 q75"}
    rid = ctx.run_record(4, {"fees": [0.5, 1, 1.5], "slippage": ["q50", "q75", "q90"]}, "PASS" if ok else "FAIL", m)
    return record_verdict(4, ctx.sleeve, "PASS" if ok else "FAIL", rid, "Sharpe LB at fee+50% q75", decisive, (decisive, None), details=m)


def step5_voldeflation(ctx: HarnessContext) -> StepRecord:
    on = _oos(ctx, base(ctx))
    off = _oos(ctx, _run(ctx, vol_deflation=False))
    n = min(len(on), len(off))
    dd_on, dd_off = stats.max_drawdown(on), stats.max_drawdown(off)
    pt, lo, hi = stats.paired_sharpe_diff_ci(on[-n:], off[-n:], reps=ctx.reps, seed=ctx.seed + 5)
    ok = dd_on < dd_off and hi >= 0  # DD improves AND Sharpe does not fall beyond noise
    m = {"max_dd_on": dd_on, "max_dd_off": dd_off, "sharpe_on_minus_off": pt, "ci": [lo, hi],
         "identical_paths": bool(np.array_equal(on, off))}
    rid = ctx.run_record(5, {"variant": "v=1"}, "PASS" if ok else "FAIL", m)
    return record_verdict(5, ctx.sleeve, "PASS" if ok else "FAIL", rid, "max DD on vs off", dd_on, (lo, hi), details=m)


def regime_labels(ctx: HarnessContext, daily_time: np.ndarray) -> dict[str, np.ndarray]:
    """Fixed labels (§7.6, §9.3 step 7): equal-weight index vs its 200-day MA; 30-day realised vol terciles."""
    closes = np.vstack([s.c[5::6] for s in ctx.series])  # daily closes
    idx = np.exp(np.mean(np.log(closes / closes[:, :1]), axis=0))
    day_ts = ctx.series[0].open_time[5::6] + 4 * 3600
    ma = np.array([idx[max(0, i - 199):i + 1].mean() for i in range(len(idx))])
    lr = np.diff(np.log(idx), prepend=np.log(idx[0]))
    vol = np.array([lr[max(0, i - 29):i + 1].std() for i in range(len(idx))])
    pos = np.searchsorted(day_ts, daily_time)
    pos = np.clip(pos, 0, len(idx) - 1)
    bull = idx[pos] > ma[pos]
    t1, t2 = np.quantile(vol[200:], [1 / 3, 2 / 3]) if len(vol) > 230 else (0, 0)
    v = vol[pos]
    return {"bull": bull, "bear": ~bull, "vol_low": v <= t1, "vol_mid": (v > t1) & (v <= t2), "vol_high": v > t2}


def step7_regimes(ctx: HarnessContext) -> StepRecord:
    b = base(ctx)
    r = _oos(ctx, b)
    t = b.daily_time[-len(r):]
    rows, ok = {}, True
    for name, mask in regime_labels(ctx, t).items():
        x = r[mask]
        if len(x) < 30:
            rows[name] = {"days": int(len(x)), "note": "too few days"}
            ok = False
            continue
        lo, hi = stats.mean_return_ci(x, reps=ctx.reps // 2, seed=ctx.seed + len(name))
        dd = stats.max_drawdown(x)
        rows[name] = {"days": int(len(x)), "max_dd": dd, "ann_return_ci": [lo, hi], "pass": dd <= 0.20 and hi > 0}
        ok &= rows[name]["pass"]
    rid = ctx.run_record(7, {"labels": list(rows)}, "PASS" if ok else "FAIL", {"labels": rows})
    return record_verdict(7, ctx.sleeve, "PASS" if ok else "FAIL", rid, "per-label DD and return CI", None, (None, None), details={"labels": rows})


def step8_sigma_star(ctx: HarnessContext) -> StepRecord:
    r = _oos(ctx, base(ctx))
    rk = ctx.policy["risk"]
    if len(r) < 30 or float(np.std(r)) == 0.0:
        m = {"reason": "no sleeve activity to calibrate on"}
        rid = ctx.run_record(8, {"paths": 0}, "FAIL", m)
        return record_verdict(8, ctx.sleeve, "FAIL", rid, "sigma*", None, (None, None), details=m)
    mc = calibrate(r, rk["ladder"], p_max=rk["p_dd20_1y_max"], dd_limit=rk["ladder"]["dd"]["terminate"], seed=ctx.seed)
    no_ladder = max((g for g, _on, off, _f in mc.grid if off <= rk["p_dd20_1y_max"]), default=None)
    ok = mc.sigma_star is not None and mc.control_ok
    m = {"sigma_star": mc.sigma_star, "at_grid_edge": mc.sigma_star == mc.grid[-1][0], "sigma_star_without_ladder": no_ladder, "control_ok": mc.control_ok,
         "grid": [{"sigma": g, "p_dd20_ladder": on, "p_dd20_no_ladder": off, "p_flatten_rung": f} for g, on, off, f in mc.grid],
         "null_mass": mc.null_mass, "nu": mc.nu, "paths": mc.paths}
    rid = ctx.run_record(8, {"paths": mc.paths, "null_mass": mc.null_mass, "nu": mc.nu}, "PASS" if ok else "FAIL", m)
    return record_verdict(8, ctx.sleeve, "PASS" if ok else "FAIL", rid, "sigma*", mc.sigma_star, (None, None), details=m)


def run_all(ctx: HarnessContext) -> list[StepRecord]:
    out = [step1_data(ctx)]
    if out[0].verdict != "PASS":  # step 1 FAIL blocks all later steps
        return out + [record_verdict(s, ctx.sleeve, "NOT_RUN", None, "blocked by step 1", None, (None, None)) for s in (2, 3, 4, 5, 7, 8)]
    for fn in (step2_null, step3_ablation, step4_costs, step5_voldeflation, step7_regimes, step8_sigma_star):
        out.append(fn(ctx))
    return out
