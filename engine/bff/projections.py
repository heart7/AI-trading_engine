"""BFF projections (spec §15.3, §16.8.2). Read-only: every function reads a PaperSession and returns JSON.

The UI computes no material figure. Every number the UI prints arrives here as a figure (claim) with its class,
interval, n_eff and data age, already passed through engine.ui.render (class-bound rendering, §16.6). Chart
geometry (bar OHLC, series points) travels as raw arrays next to the figures; the client scales them to pixels
and does nothing else with them.
"""
from __future__ import annotations

import math
from collections import Counter
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import yaml

from engine.bff import activity as act
from engine.bff.session import BPD, H4, WINDOW_BARS, PaperSession, base_of, pair_name, t_band
from engine.evidence.performance import UnitFund
from engine.evidence.stress import Book, run_battery, time_to_flatten_min
from engine.evidence.stress import Position as StressPosition
from engine.execution.registry import ConnectorRegistry
from engine.regime.ccmrm import STATES, information_horizon
from engine.risk.gates import LADDER_ORDER
from engine.router.router import TIERS, TierEvidence, eligibility
from engine.ui.render import RenderViolation, figure, fmt_number, render
from research.harness.verdicts import VerdictRejected, record_verdict

ROOT = Path(__file__).resolve().parents[2]
TIER_NAMES = {"T0": "No trading", "T1": "Micro", "T2": "Spot Core", "T3": "Core+Short", "T4": "Two-Sided"}
MICRO_NOTICE = "Micro tier: expected to lose money after operating costs; purpose is operational validation"
BAR_TTL_S = H4 + 300
STREAM_TTL_S = 30
FUNNEL_STEPS = [("evaluated", "Bars evaluated"), ("data_admissible", "Data admissible"), ("breakout", "B > 0"),
                ("t_entry", "T ≥ 0.5"), ("cost_gate", "Evidence + cost gate"), ("risk_caps", "Risk and cluster caps"),
                ("entered", "Entered")]
# first failing gate -> the funnel step it removes the bar from (LADDER_ORDER is §7.1)
GATE_STEP = {g: "data_admissible" for g in LADDER_ORDER[:6]}
GATE_STEP.update({"NO_BREAKOUT": "breakout", "T_BELOW_ENTRY": "t_entry", "EVIDENCE_NOT_ON_FILE": "cost_gate",
                  "COST_INPUT_STALE": "cost_gate", "COST_R_EXCEEDED": "cost_gate"})
GATE_STEP.update({g: "risk_caps" for g in LADDER_ORDER[11:]})
LOSS_CONSEQUENCE = {"daily": "STOP entries to 00:00 UTC + daily review", "5d": "SUSPEND · reduce-only · signed re-arm",
                    "20d": "per-trade risk halved for 20 days", "DD_8": "halve size until DD < 5%",
                    "DD_12": "SUSPEND + tier downgrade · signed re-arm", "DD_16": "FLATTEN decision within the dead-man window",
                    "DD_20": "TERMINATE · flatten · signed re-arm"}
STEP_FALSIFICATION = {1: "coverage < 99% or quarantine > 1% -> dataset not certified",
                      2: "Sharpe CI lower bound <= 0 or DSR <= 0.95 -> sleeve weight 0 (§9.5)",
                      3: "paired CI includes 0 -> delete that component by proposal",
                      4: "net edge < 0 at q90 costs -> no trade at this tier",
                      5: "no DD improvement beyond MDE -> vol deflation off",
                      6: "regime layer stays T0 (never gates SHADOW)",
                      7: "negative in any confirmed regime beyond budget -> regime-conditional weight 0",
                      8: "sigma* outside the stable band -> anchor stays ASSUMED"}


# ---------------------------------------------------------------- helpers
def F(s: PaperSession, fid: str, label: str, value: Any, cls: str, **kw: Any) -> dict[str, Any]:
    kw.setdefault("now", s.clock)
    return render(figure(fid, label, value, cls, **kw), n_eff_floor=int(s.policy["ui"]["n_eff_warning_floor"]))


def _fnum(x: Any) -> float | None:
    if x is None:
        return None
    x = float(x)
    return None if math.isnan(x) else x


def _series_list(a: np.ndarray, digits: int = 6) -> list[float | None]:
    return [None if (v is None or (isinstance(v, float) and math.isnan(v))) else round(float(v), digits) for v in a]


def active_tier(s: PaperSession) -> str:
    """Tier whose rules the paper session simulates (the fund itself is at T0 until signed)."""
    return "T2"


def resolve_pair(s: PaperSession, pair: str) -> str:
    return s.by_id(pair).instrument_id


def _closes_idx(s: PaperSession) -> tuple[int, np.ndarray]:
    i = s.last_index()
    return i, s.series[0].open_time[: i + 1] + H4


# ---------------------------------------------------------------- activity: cycles
def _entry_trade_index(s: PaperSession) -> dict[tuple[str, int], Any]:
    return {(t.instrument_id, t.entry_time): t for t in s.result.trades}


def cycle_events(s: PaperSession, i: int) -> list[dict[str, Any]]:
    """All events of the cycle closing bar i (unmasked within the cycle; routes mask by cursor)."""
    close = s.bar_close(i)
    cts = int(close.timestamp())
    iso = close.isoformat()
    nxt = cts + H4
    extra = [{k: v for k, v in e.items() if k != "cycle_index"} for e in s.extra_activity if e.get("cycle_index") == i]
    for e in extra:
        e.setdefault("outcome", "injected"); e.setdefault("ref_kind", "claim")  # noqa: E702
    for r in s.reporter_log:
        at = int(datetime.fromisoformat(r["at"]).timestamp())
        if cts <= at < nxt:
            extra.append({"agent": "reporter", "cls": "INFORM", "subject": "reporter_answer", "t_ms": (at - cts) * 1000,
                          "outcome": ("refused: EXECUTE request" if r["refused"] else "answered with citations"),
                          "reason": "EXECUTE_REQUEST" if r["refused"] else None, "ref_kind": "claim", "ref_id": r["id"]})
    cache = s.cycle_cache
    if i in cache and not extra:
        return cache[i]
    insts = [x.instrument_id for x in s.series]
    intents = [x for x in s.result.intents_tail if x["bar_close"] == iso]
    sigs = {k: {"T": _fnum(s.result.signals[k]["T"][i]), "B": _fnum(s.result.signals[k]["B"][i])} for k in insts}
    trades_by_entry = _entry_trade_index(s)
    filled: dict[str, dict] = {}
    last = len(s.series[0].c) - 1
    for x in intents:
        if x["outcome"] == "ENTERED":
            t = trades_by_entry.get((x["instrument_id"], cts))
            if i == last:
                filled[x["instrument_id"]] = {"filled": None}
            else:
                filled[x["instrument_id"]] = {"filled": t is not None, "liquidity": getattr(t, "entry_liquidity", "")}
    exits, stop_fills = {}, {}
    for t in s.result.trades:
        if t.exit_time == cts and t.exit_reason not in ("INITIAL_STOP", "TRAILING_STOP", "END_OF_REPLAY"):
            exits[t.instrument_id] = t.exit_reason
        if t.exit_time + H4 == cts and t.exit_reason in ("INITIAL_STOP", "TRAILING_STOP"):
            stop_fills[t.instrument_id] = t.exit_reason
    changes = []
    for k, hist in s.result.stops.items():
        for j in range(1, len(hist)):
            if hist[j][0] == cts and hist[j - 1][0] == cts - H4 and hist[j][1] != hist[j - 1][1]:
                changes.append(k)
    evs = act.build_cycle(cycle_close=close, bar_index=i, instruments=insts, intents=intents, signals=sigs,
                          regime_authority=s.policy["regime"]["authority"], policy_hash=s.policy_hash,
                          entries_filled=filled, exits=exits, stop_changes=changes, stop_fills=stop_fills, extra=extra)
    if not extra:
        cache[i] = evs
    return evs


def _visible(s: PaperSession, evs: list[dict], cursor_ms: int | None) -> list[dict]:
    cut = s.clock.isoformat()
    out = [e for e in evs if e["created_at"] <= cut]
    if cursor_ms is not None:
        out = [e for e in out if e["t_offset_ms"] <= cursor_ms]
    return out


def _stream_figure(s: PaperSession) -> dict[str, Any]:
    alive = s.stream_alive
    return F(s, "activity-stream", "Activity stream", "live" if alive else "disconnected", "OBSERVED",
             observed_at=s.stream_last_good if not alive else s.clock, ttl_s=STREAM_TTL_S,
             state=None if alive else "STALE", source="agents.activity.* (paper session)")


def activity_cycles(s: PaperSession, limit: int = 6) -> dict[str, Any]:
    i = s.last_index()
    rows = []
    for j in range(i, max(-1, i - limit), -1):
        evs = _visible(s, cycle_events(s, j), None)
        tm = act.step_timings(evs)
        rows.append({"cycle_id": s.bar_close(j).isoformat(), "bar_index": j, "events": len(evs),
                     "steps": [{"step": k, "label": act.STEP_LABEL[k], "t_ms": tm[k], "deadline_ms": act.DEADLINES_MS[k],
                                "late": tm[k] is not None and tm[k] > act.DEADLINES_MS[k]} for k in act.STEP_ORDER],
                     "late": any(e["late"] for e in evs)})
    return {"cycles": rows, "stream": _stream_figure(s), "fixture": True}


def _find_cycle(s: PaperSession, cycle_id: str) -> int:
    closes = s.series[0].open_time + H4
    ts = int(datetime.fromisoformat(cycle_id).timestamp())
    j = int(np.searchsorted(closes, ts))
    if j >= len(closes) or int(closes[j]) != ts or j > s.last_index():
        raise KeyError(cycle_id)
    return j


def activity_cycle(s: PaperSession, cycle_id: str, cursor_ms: int | None = None) -> dict[str, Any]:
    j = _find_cycle(s, cycle_id)
    evs = _visible(s, cycle_events(s, j), cursor_ms)
    tier = active_tier(s)
    lanes = act.lanes(s.policy, tier)
    # P1 marks and the P1a log are drawn from this one list, so the two always agree
    marks = [{**e, "t_s": e["t_offset_ms"] / 1000, "timeout_marker": e["late"],
              "shape": {"INFORM": "dot", "RECOMMEND": "diamond", "PROPOSE": "square", "EXECUTE": "triangle",
                        "ABSTAIN": "hollow-circle"}[e["emission_class"]],
              "pair": pair_name(e["instrument_id"]) if e["instrument_id"] else "all",
              "t_label": _t_label(e["t_offset_ms"])} for e in evs]
    return {"cycle_id": cycle_id, "cursor_ms": cursor_ms, "lanes": lanes, "events": marks,
            "deadlines": [{"step": k, "label": "entry deadline" if k == "entry_ladder" else k, "t_ms": v}
                          for k, v in act.DEADLINES_MS.items()],
            "axis": {"split_s": 180, "max_s": max([900] + [math.ceil(e["t_offset_ms"] / 1000) for e in evs]), "split_share": 0.7, "ticks_s": [0, 30, 60, 90, 120, 150, 180, 300, 600, 900]},
            "edges": act.flow_edges(evs, s.policy["regime"]["authority"]),
            "regime_authority": s.policy["regime"]["authority"], "stream": _stream_figure(s), "fixture": True}


def _t_label(ms: int) -> str:
    sec = ms // 1000
    if sec < 180:
        return f"t+{sec}s"
    m, r = divmod(sec, 60)
    return f"t+{m}m" + (f"{r:02d}s" if r else "")


# ---------------------------------------------------------------- activity: pair
def _donchian(c: np.ndarray, L_bars: int, idx: np.ndarray) -> tuple[list, list]:
    hi, lo = [], []
    for i in idx:
        if i - L_bars < 0:
            hi.append(None)
            lo.append(None)
            continue
        w = c[i - L_bars:i]
        hi.append(round(float(w.max()), 8))
        lo.append(round(float(w.min()), 8))
    return hi, lo


def cost_r(s: PaperSession, inst: str, i: int) -> float | None:
    sig = s.result.signals[inst]
    a = _fnum(sig["atr_daily"][i])
    if a is None:
        return None
    px = float(s.by_id(inst).c[i])
    d = s.policy["stops"]["k_stop"] * a / px
    c = s.costs.roundtrip_fee + 2 * s.costs.half_spread + s.costs.slippage_q75
    return c / d if d > 0 else None


def stop_distance(s: PaperSession, inst: str, i: int) -> float | None:
    a = _fnum(s.result.signals[inst]["atr_daily"][i])
    return None if a is None else s.policy["stops"]["k_stop"] * a / float(s.by_id(inst).c[i])


def latest_intent(s: PaperSession, inst: str) -> dict | None:
    xs = [x for x in s.intents_visible() if x["instrument_id"] == inst]
    return xs[-1] if xs else None


def regime_claim_at(s: PaperSession, inst: str) -> dict | None:
    if s.cursor is None:
        return s.regime_claims[inst]
    from engine.regime.ccmrm import RegimeLayer
    i = s.last_index()
    return RegimeLayer(s.policy, s.policy_hash).claim(inst, s.by_id(inst).c, i, s.bar_close(i), now=s.clock)


def activity_pair(s: PaperSession, pair: str, bars: int = WINDOW_BARS) -> dict[str, Any]:
    inst = resolve_pair(s, pair)
    ser = s.by_id(inst)
    i = s.last_index()
    bars = max(10, min(int(bars), 2000))
    idx = np.arange(max(0, i - bars + 1), i + 1)
    sig = s.result.signals[inst]
    lo_b, hi_b = t_band(ser.c, sig, s.policy, idx)
    times = [datetime.fromtimestamp(int(ser.open_time[j]) + H4, tz=timezone.utc).isoformat() for j in idx]
    don = {}
    for L in s.policy["signal"]["breakout_lookbacks_d"]:
        h, lw = _donchian(ser.c, L * BPD, idx)
        don[str(L)] = {"hi": h, "lo": lw}
    cut = int(s.clock.timestamp())
    t0 = int(ser.open_time[idx[0]]) + H4
    # stops: one segment per position, active stop per bar close, as seen at the clock
    segs = []
    closed, open_ = s.trades_visible()
    for t in closed + open_:
        if t.instrument_id != inst:
            continue
        end = min(t.exit_time if t in closed else cut, cut)
        pts = [(ts, v) for ts, v in s.result.stops[inst] if t.entry_time < ts <= end and ts >= t0]
        if pts:
            seg = {"entry_time": t.entry_time, "points": [[datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(), round(v, 8)]
                                                          for ts, v in pts]}
            # render check: raises STOP_MOVED_AGAINST_POSITION if the stop ever moved against the long position
            F(s, f"stop-{inst}-{t.entry_time}", "Active stop", [v for _, v in pts], "DERIVED", kind="stop_series", extra={"side": 1})
            seg["monotone"] = True
            segs.append(seg)
    markers = []
    for t in closed + open_:
        if t.instrument_id != inst:
            continue
        if t.entry_time >= t0 - H4:
            markers.append({"kind": "entry", "time": datetime.fromtimestamp(t.entry_time, tz=timezone.utc).isoformat(),
                            "price": round(t.entry_px, 8), "label": f"entry · {t.entry_liquidity}"})
        if t in closed and t.exit_time >= t0 - H4:
            markers.append({"kind": "exit", "time": datetime.fromtimestamp(t.exit_time, tz=timezone.utc).isoformat(),
                            "price": round(t.exit_px, 8), "label": f"exit · {t.exit_reason} · {t.R:+.2f}R"})
    for x in s.intents_visible():
        if x["instrument_id"] != inst or x["outcome"] == "ENTERED" or x["bar_close"] < times[0]:
            continue
        T = next((r for r in x["gate_ladder"] if r["gate"] == "T_BELOW_ENTRY"), None)
        if x["binding_gate"] in ("NO_BREAKOUT", "COST_R_EXCEEDED", "COST_INPUT_STALE", "RISK_BUDGET", "EVIDENCE_NOT_ON_FILE") and \
                T and T["value"] is not None and T["value"] >= s.policy["signal"]["T_entry"]:
            markers.append({"kind": "declined", "time": x["bar_close"], "gate": x["binding_gate"],
                            "label": f"declined · {x['binding_gate']}"})
    reg = [s.regime_states[inst][j] for j in idx]
    gaps = [int(ser.open_time[j] - ser.open_time[j - 1]) != H4 for j in idx if j > 0]
    # axis and tooltips are formatted here so the client never formats a figure
    vals = [float(ser.l[idx].min()), float(ser.h[idx].max())] + [v for v in don[str(min(don, key=int))]["lo"] if v is not None]
    vals += [v for seg in segs for _, v in seg["points"]]
    lo_p, hi_p = min(vals), max(vals)
    pad = (hi_p - lo_p) * 0.06
    lo_p, hi_p = lo_p - pad, hi_p + pad
    ticks = [{"v": lo_p + (hi_p - lo_p) * g / 4, "label": fmt_number(lo_p + (hi_p - lo_p) * g / 4, "px")} for g in range(5)]
    tips = []
    for n, j in enumerate(idx):
        def g(k: str, j: int = j) -> str:
            v = _fnum(sig[k][j])
            return "—" if v is None else f"{v:+.2f}"
        tips.append(f"O {fmt_number(float(ser.o[j]), 'px')}  H {fmt_number(float(ser.h[j]), 'px')}  L {fmt_number(float(ser.l[j]), 'px')}  "
                    f"C {fmt_number(float(ser.c[j]), 'px')}|T {g('T')} ({fmt_number(_fnum(lo_b[n]), digits=2)} to {fmt_number(_fnum(hi_b[n]), digits=2)}) · "
                    f"B {g('B')} · M {g('M')} · Z {g('Z')}|regime {reg[n] or '—'} (confirmed)")
    return {"pair": pair_name(inst), "instrument_id": inst, "bars": {
        "time": times, "o": _series_list(ser.o[idx], 8), "h": _series_list(ser.h[idx], 8), "l": _series_list(ser.l[idx], 8),
        "c": _series_list(ser.c[idx], 8), "gaps": sum(gaps), "tip": tips,
        "x_ticks": [{"i": n, "label": t[5:10]} for n, t in enumerate(times) if n % 30 == 0]},
        "price_axis": {"lo": lo_p, "hi": hi_p, "ticks": ticks},
        "donchian": don, "stops": segs, "markers": markers, "regime": reg,
        "T": {"T": _series_list(sig["T"][idx]), "B": _series_list(sig["B"][idx]), "M": _series_list(sig["M"][idx]),
              "Z": _series_list(sig["Z"][idx]), "lo": _series_list(lo_b), "hi": _series_list(hi_b),
              "entry": s.policy["signal"]["T_entry"], "class": "ESTIMATED", "band_basis": "A-T-BAND: sigma x 0.8 / 1.25"},
        "rail": pair_rail(s, inst), "fixture": True}


def pair_rail(s: PaperSession, inst: str) -> dict[str, Any]:
    i = s.last_index()
    ser = s.by_id(inst)
    sig = s.result.signals[inst]
    close = s.bar_close(i)
    lo, hi = t_band(ser.c, sig, s.policy, np.array([i]))
    T = _fnum(sig["T"][i])
    rc = regime_claim_at(s, inst)
    closed, open_ = s.trades_visible()
    pos = next((t for t in open_ if t.instrument_id == inst), None)
    it = latest_intent(s, inst)
    stops = [v for ts, v in s.result.stops[inst] if ts <= int(close.timestamp())]
    auth = s.policy["regime"]["authority"]
    fig = []
    obs = {"observed_at": close, "ttl_s": BAR_TTL_S, "source": "paper session · FIXTURE bars"}
    fig.append(F(s, f"px-{inst}", "Close", float(ser.c[i]), "OBSERVED", unit="px", **obs))
    fig.append(F(s, f"T-{inst}", "Trend score T (90% band)", T, "ESTIMATED",
                 interval=(float(lo[0]), float(hi[0])) if T is not None else None, digits=2, **obs))
    for k in ("B", "M", "Z"):
        fig.append(F(s, f"{k}-{inst}", k, _fnum(sig[k][i]), "DERIVED", digits=2, lineage=["closes", "policy.signal"], **obs))
    if rc:
        p = rc["payload"]
        fig.append(F(s, f"regime-{inst}", "Confirmed regime", p["state"], "ESTIMATED",
                     interval=(p["P_next"][p["state"]]["lo90"], p["P_next"][p["state"]]["hi90"]), n_eff=p["ESS"][STATES.index(p["state"])],
                     **obs))
        fig.append(F(s, f"m-{inst}", "m_regime", f"{p['m_regime']:.2f} · computed, not applied" if auth == "T0" else p["m_regime"],
                     "DERIVED", kind="m_regime" if auth != "T0" else None, **obs))
    cr = cost_r(s, inst, i)
    fig.append(F(s, f"costR-{inst}", "cost_R at q75", cr, "DERIVED", digits=3, lineage=["A-FEES-KRAKEN", "spread", "slippage q75"],
                 extra={"limit": s.policy["cost"]["cost_R_max"], "limit_display": fmt_number(s.policy["cost"]["cost_R_max"], digits=2),
                        "breach": cr is not None and cr > s.policy["cost"]["cost_R_max"]}, **obs))
    fig.append(F(s, f"d-{inst}", "Stop distance d", stop_distance(s, inst, i), "DERIVED", unit="%", digits=1, **obs))
    if pos:
        openR = (float(ser.c[i]) - pos.entry_px) / (pos.entry_px - stops[0]) if stops and pos.entry_px > stops[0] else None
        fig.append(F(s, f"openR-{inst}", "Open R", openR, "DERIVED", digits=2, **obs))
        fig.append(F(s, f"trail-{inst}", "Active stop", stops[-1] if stops else None, "DERIVED", unit="px", **obs))
        fig.append(F(s, f"prot-{inst}", "Protection", "venue stop ✓ (paper sim read-back)", "OBSERVED", **obs))
    gate = "IN_POSITION" if pos else (it["binding_gate"] or "ENTRY_SIGNAL") if it else "NO_INTENT"
    fig.append(F(s, f"gate-{inst}", "Binding gate", gate, "DERIVED", lineage=["signal_intent.gate_ladder"], **obs))
    nxt = close + timedelta(seconds=H4)
    window_start = int(s.series[0].open_time[max(0, i - WINDOW_BARS + 1)])
    eps = [t for t in closed if t.instrument_id == inst and t.exit_time >= window_start]
    return {"figures": fig, "next_evaluation": nxt.isoformat(), "authority": auth, "in_position": pos is not None,
            "episodes": [{"entry": datetime.fromtimestamp(t.entry_time, tz=timezone.utc).isoformat(),
                          "exit": datetime.fromtimestamp(t.exit_time, tz=timezone.utc).isoformat(),
                          "R": F(s, f"ep-{inst}-{t.entry_time}", "R", t.R, "DERIVED", digits=2)} for t in eps],
            "stream": _stream_figure(s)}


def activity_universe(s: PaperSession) -> dict[str, Any]:
    i = s.last_index()
    cards = []
    order = [f"FIXTURE_{b}" for b in list(s.policy["universe"]["A"]["mandatory"]) + list(s.policy["universe"]["A"]["default"])]
    _closed, open_ = s.trades_visible()
    for inst in order:
        ser = s.by_id(inst)
        idx = np.arange(max(0, i - WINDOW_BARS + 1), i + 1)
        sig = s.result.signals[inst]
        lo, hi = t_band(ser.c, sig, s.policy, np.array([i]))
        it = latest_intent(s, inst)
        pos = next((t for t in open_ if t.instrument_id == inst), None)
        chg = float(ser.c[i] / ser.c[idx[0]] - 1)
        close = s.bar_close(i)
        T = _fnum(sig["T"][i])
        cards.append({"pair": pair_name(inst), "instrument_id": inst, "spark": _series_list(ser.c[idx], 8),
                      "change_30d": F(s, f"chg-{inst}", "30d change", chg, "DERIVED", unit="%", digits=1, observed_at=close, ttl_s=BAR_TTL_S),
                      "T": F(s, f"Tc-{inst}", "T", T, "ESTIMATED", interval=(float(lo[0]), float(hi[0])) if T is not None else None,
                             digits=2, observed_at=close, ttl_s=BAR_TTL_S),
                      "regime": s.regime_states[inst][i],
                      "gate": {"kind": "pos" if pos else ("pass" if it and it["binding_gate"] is None else "block"),
                               "text": "in position" if pos else ("ELIGIBLE" if it and it["binding_gate"] is None
                                                                  else (it["binding_gate"] if it else "NO_INTENT"))}})
    return {"cards": cards, "order": "policy universe order (never by performance)", "fixture": True}


# ---------------------------------------------------------------- activity: funnel / timing / emissions
def funnel(s: PaperSession, frm: datetime | None = None, to: datetime | None = None) -> dict[str, Any]:
    xs = s.intents_visible()
    if frm is not None:
        xs = [x for x in xs if x["bar_close"] >= frm.isoformat()]
    if to is not None:
        xs = [x for x in xs if x["bar_close"] <= to.isoformat()]
    lost: dict[str, Counter] = {k: Counter() for k, _ in FUNNEL_STEPS}
    for x in xs:
        g = x["binding_gate"]
        if g is not None:
            lost[GATE_STEP[g]][g] += 1
    counts, n = [], len(xs)
    for k, label in FUNNEL_STEPS:
        if k != "evaluated":
            n -= sum(lost[k].values())
        top = lost[k].most_common(1)
        counts.append({"step": k, "label": label, "count": n, "lost": sum(lost[k].values()),
                       "top_reason": top[0][0] if top else None})
    total_lost = sum(c["lost"] for c in counts)
    ok = all(a["count"] >= b["count"] for a, b in zip(counts, counts[1:], strict=False)) and \
        total_lost == counts[0]["count"] - counts[-1]["count"]
    if not ok:
        raise RenderViolation("FUNNEL_DOES_NOT_SUM", str(counts))
    return {"from": xs[0]["bar_close"] if xs else None, "to": xs[-1]["bar_close"] if xs else None, "steps": counts,
            "sums": True, "source": "signal_intent.gate_ladder", "fixture": True}


def _cycles_in_days(s: PaperSession, days: int) -> list[int]:
    i = s.last_index()
    n = min(days * BPD, WINDOW_BARS)
    return list(range(max(0, i - n + 1), i + 1))


def timing(s: PaperSession, days: int = 30) -> dict[str, Any]:
    per: dict[str, list[int]] = {k: [] for k in act.STEP_ORDER}
    for j in _cycles_in_days(s, days):
        for k, v in act.step_timings(_visible(s, cycle_events(s, j), None)).items():
            if v is not None:
                per[k].append(v)
    rows = []
    for k in act.STEP_ORDER:
        v = np.array(per[k]) if per[k] else None
        p50 = int(np.percentile(v, 50)) if v is not None else None
        p95 = int(np.percentile(v, 95)) if v is not None else None
        warn = p95 is not None and p95 > act.DEADLINES_MS[k]
        rows.append({"step": k, "label": act.STEP_LABEL[k], "p50_ms": p50, "p95_ms": p95, "deadline_ms": act.DEADLINES_MS[k],
                     "n": len(per[k]), "warn": warn, "incident": "CYCLE_TIMEOUT" if warn else None,
                     "display": f"p50 {_t_label(p50) if p50 is not None else '—'} · p95 {_t_label(p95) if p95 is not None else '—'}"})
    return {"days": days, "steps": rows, "fixture": True, "clock": "A-UI-CYCLE-CLOCK (FIXTURE cycle clock)"}


def emissions_projection(s: PaperSession, days: int = 30) -> dict[str, Any]:
    em = act.emissions(_visible(s, cycle_events(s, j), None) for j in _cycles_in_days(s, days))
    lanes = act.lanes(s.policy, active_tier(s))
    rows = []
    for L in lanes:
        c = em[L["id"]]
        rows.append({"agent": L["id"], "name": L["name"], "active": L["active"], "tier_label": L["tier_label"],
                     "counts": {k: int(c.get(k, 0)) for k in ("INFORM", "RECOMMEND", "PROPOSE", "EXECUTE", "ABSTAIN")},
                     "total": int(sum(c.values()))})
    return {"days": days, "agents": rows, "classes": ["INFORM", "RECOMMEND", "PROPOSE", "EXECUTE", "ABSTAIN"],
            "completeness": "every claim, intent, order transition and read-back has exactly one event", "fixture": True}


# ---------------------------------------------------------------- risk (single source for the loss ladder)
def _daily_navs(s: PaperSession) -> list[tuple[int, float]]:
    et, eq = s.equity_visible()
    return [(int(t), float(v)) for t, v in zip(et, eq, strict=True) if int(t) % 86400 == 0]


def loss_state(s: PaperSession) -> dict[str, float]:
    et, eq = s.equity_visible()
    nav = float(eq[-1]) if len(eq) else s.nav0
    daily = [v for _, v in _daily_navs(s)]
    day_start = daily[-1] if daily else s.nav0

    def over(d: int) -> float:
        if len(daily) <= d:
            return max(0.0, 1 - nav / s.nav0) if daily else 0.0
        return max(0.0, 1 - nav / daily[-d - 1])
    hwm = max([s.nav0] + [float(x) for x in eq])
    return {"nav": nav, "loss_today": max(0.0, 1 - nav / day_start), "loss_5d": over(5), "loss_20d": over(20),
            "drawdown": 1 - nav / hwm, "hwm": hwm}


def loss_ladder(s: PaperSession) -> list[dict[str, Any]]:
    L = s.policy["risk"]["ladder"]
    st = loss_state(s)
    close = s.bar_close(s.last_index())
    rows = [("daily", "Daily loss", st["loss_today"], L["daily_stop"]), ("5d", "5-day rolling", st["loss_5d"], L["rolling_5d_suspend"]),
            ("20d", "20-day rolling", st["loss_20d"], L["rolling_20d_halve_r"])]
    out = []
    for key, label, v, lim in rows:
        u = v / lim if lim else 0.0
        out.append({"key": key, "label": label, "value": F(s, f"loss-{key}", label, v, "DERIVED", unit="%", observed_at=close, ttl_s=BAR_TTL_S),
                    "limit": F(s, f"lim-{key}", f"{label} limit", lim, "DERIVED", unit="%", digits=0, source="policy.risk.ladder"),
                    "utilisation": round(u, 4), "status": "breach" if u >= 1 else "warn" if u >= 0.75 else "ok",
                    "consequence": LOSS_CONSEQUENCE[key]})
    dd = st["drawdown"]
    rungs = [("DD_8", L["dd"]["halve"]), ("DD_12", L["dd"]["suspend_downgrade"]), ("DD_16", L["dd"]["flatten_decision"]),
             ("DD_20", L["dd"]["terminate"])]
    nxt = next(((n, v) for n, v in rungs if dd < v), None)
    out.append({"key": "dd", "label": "Drawdown", "value": F(s, "loss-dd", "Drawdown", dd, "DERIVED", unit="%", observed_at=close, ttl_s=BAR_TTL_S),
                "limit": F(s, "lim-dd", "Hard drawdown", L["dd"]["terminate"], "DERIVED", unit="%", digits=0, source="policy.risk.ladder.dd"),
                "rungs": [{"rung": n, "level": v, "display": fmt_number(v, "%", 0), "consequence": LOSS_CONSEQUENCE[n], "hit": dd >= v}
                          for n, v in rungs],
                "utilisation": round(dd / L["dd"]["terminate"], 4),
                "status": "breach" if dd >= L["dd"]["terminate"] else "warn" if dd >= L["dd"]["halve"] else "ok",
                "next_rung": nxt[0] if nxt else None,
                "consequence": LOSS_CONSEQUENCE[nxt[0]] if nxt else LOSS_CONSEQUENCE["DD_20"]})
    return out


def _book(s: PaperSession) -> tuple[list, float, float]:
    _c, open_ = s.trades_visible()
    i = s.last_index()
    nav = loss_state(s)["nav"]
    pos = []
    for t in open_:
        px = float(s.by_id(t.instrument_id).c[i])
        stops = [v for ts, v in s.result.stops[t.instrument_id] if t.entry_time < ts <= int(s.bar_close(i).timestamp())]
        stop = stops[-1] if stops else None
        pos.append({"t": t, "px": px, "notional": t.qty * px, "stop": stop,
                    "risk": max(0.0, t.qty * (px - stop)) if stop else None})
    gross = math.fsum(p["notional"] for p in pos)
    return pos, gross, nav


def risk(s: PaperSession) -> dict[str, Any]:
    pos, gross, nav = _book(s)
    close = s.bar_close(s.last_index())
    open_risk = math.fsum(p["risk"] or 0 for p in pos)
    sz, rk = s.policy["sizing"], s.policy["risk"]
    tier = active_tier(s)
    r_tier = s.policy["tiers"][tier]["r"]["A"]
    book = Book(nav, tuple(StressPosition(base_of(p["t"].instrument_id), "paper", p["notional"] / nav,
                                          (p["px"] - p["stop"]) / p["px"] if p["stop"] else 0.08) for p in pos) or
                (StressPosition("BTC", "paper", 0.0, 0.08),), {"paper": s.policy["collateral"]["spot_operating_buffer"]})
    ttf = time_to_flatten_min(book)
    obs = {"observed_at": close, "ttl_s": BAR_TTL_S}
    exposure = [
        {"label": "Per-trade risk", "value": F(s, "r-tier", "Per-trade risk", r_tier, "DERIVED", unit="%", source=f"policy.tiers.{tier}.r.A"),
         "limit": F(s, "r-tier-lim", "limit", r_tier, "DERIVED", unit="%"), "consequence": "order rejected at gate"},
        {"label": "Cluster open risk", "value": F(s, "cluster", "Cluster open risk", open_risk / nav, "DERIVED", unit="%", **obs),
         "limit": F(s, "cluster-lim", "limit", sz["cluster"]["open_risk_cap"], "DERIVED", unit="%"),
         "consequence": "new entries in cluster rejected"},
        {"label": "Venue exposure", "value": F(s, "venue-exp", "Venue exposure", gross / nav, "DERIVED", unit="%", **obs),
         "limit": F(s, "venue-lim", "limit", rk["venue_exposure_max"], "DERIVED", unit="%", digits=0),
         "consequence": "entries on venue blocked"},
    ]
    return {
        "loss_ladder": loss_ladder(s), "exposure": exposure,
        "es975": F(s, "es975", "ES97.5 multiplier", rk["es975_mult"], "ASSUMED", owner="principal", review_by="after P2b (decision 0002)"),
        "p_ruin": F(s, "p-dd20", "P(DD ≥ 20%, 1y)", None, "ESTIMATED", state="MISSING",
                    reason="needs certified history and step 8 (sigma*)", extra={"limit": rk["p_dd20_1y_max"]}),
        "margin_invariant": {"status": "not applicable", "reason": f"Strategy B inactive at {tier}; spot posts no margin"},
        "adl": {"status": "not applicable", "reason": "spot only at this tier"},
        "circuit_breakers": [], "sigma_star": F(s, "sigma-star", "sigma*", sz["sigma_star"]["value"] or sz["sigma_star"]["anchor"],
                                                  "ASSUMED", owner="principal", review_by="step 8", digits=2),
        "flatten_preview": {"time_to_flatten_min": F(s, "ttf", "Time to flatten", ttf, "ESTIMATED", interval=(ttf, ttf * 3.0),
                                                     digits=1, extra={"basis": "A-STRESS-BOOK: 10% participation, 0.3 stress depth"}),
                            "ceiling_min": rk["ttf_ceiling_min"]["A"]},
        "kill_switches": [{"switch": k, "scope": "engine", "armed": True, "control": "Governance (hardware-key approval)"}
                          for k in ("STOP", "SUSPEND", "FLATTEN")],
        "rearm_rule": "Re-arm needs a hardware-key approval and a written rationale; no control here raises a limit.",
        "fixture": True}


# ---------------------------------------------------------------- top bar and fund room
def nav_verification(s: PaperSession) -> dict[str, Any]:
    ev = s.evidence
    masked = s.cursor is not None
    if masked:
        return {"status": "provisional", "text": "NAV provisional (playback)", "record": None}
    ok = bool(ev["passed"])
    return {"status": "reconciled" if ok else "divergent", "text": ("NAV ✓ dual path" if ok else "NAV ⚠ divergent → incident"),
            "record": {"ledger_net": ev["ledger_net"], "attribution_net": ev["attribution_net"], "ledger_chain_ok": ev["ledger_chain_ok"]}}


def topbar(s: PaperSession) -> dict[str, Any]:
    tier = active_tier(s)
    pos, gross, nav = _book(s)
    open_risk = math.fsum(p["risk"] or 0 for p in pos) / nav
    stale = 0 if s.stream_alive else 1
    conflicts = [m for m in bots(s, light=True)["modules"] if m["conflicted"]]
    inc = [i for i in s.incidents.items if i.open]
    return {"mode": "PAPER", "engine_state": "RUNNING" if s.stream_alive else "DEGRADED",
            "strategy_chip": f"{tier} · {TIER_NAMES[tier]} (paper tier) · A-long {len(pos)}/{s.policy['tiers'][tier]['n_max']['A']} · "
                             f"risk {fmt_number(open_risk, '%', 1)} of {fmt_number(s.policy['sizing']['cluster']['open_risk_cap'], '%', 1)}",
            "conflicts": len(conflicts), "stale": stale, "open_incidents": len(inc), "nav": nav_verification(s),
            "fixture": True, "clock": s.clock.isoformat()}


def fund_room(s: PaperSession) -> dict[str, Any]:
    st = loss_state(s)
    tier = active_tier(s)
    close = s.bar_close(s.last_index())
    obs = {"observed_at": close, "ttl_s": BAR_TTL_S}
    pos, gross, nav = _book(s)
    daily = [v for _, v in _daily_navs(s)]

    def pnl(d: int) -> float:
        return nav - daily[-d - 1] if len(daily) > d else nav - s.nav0
    month_start = close.replace(day=1, hour=0, minute=0, second=0)
    mtd_base = next((v for t, v in _daily_navs(s) if t >= int(month_start.timestamp())), s.nav0)
    ladder = loss_ladder(s)
    nav_fig = F(s, "nav", "NAV", nav, "DERIVED", unit="USD", lineage=["ledger", "replay marks"],
                verification=nav_verification(s), **obs)
    positions = [{"pair": pair_name(p["t"].instrument_id), "sleeve": "A_long",
                  "size": F(s, f"pos-{p['t'].instrument_id}", "Notional", p["notional"], "DERIVED", unit="USD", **obs),
                  "r_at_risk": F(s, f"rar-{p['t'].instrument_id}", "R at risk", (p["risk"] or 0) / nav, "DERIVED", unit="%", **obs),
                  "stop_distance": F(s, f"sd-{p['t'].instrument_id}", "Stop distance", (p["px"] - p["stop"]) / p["px"] if p["stop"] else None,
                                     "DERIVED", unit="%", digits=1, **obs),
                  "protection": "venue stop ✓ (paper)", "m_applied": "no · computed, not applied (T0)"} for p in pos]
    why = []
    for x in s.series:
        it = latest_intent(s, x.instrument_id)
        inpos = any(p["t"].instrument_id == x.instrument_id for p in pos)
        why.append({"pair": pair_name(x.instrument_id), "in_position": inpos,
                    "binding_gate": None if inpos else (it["binding_gate"] if it else "NO_INTENT"),
                    "ladder": [] if inpos or not it else [{"gate": r["gate"], "passed": r["passed"],
                                                           "value": None if r["value"] is None else fmt_number(r["value"], digits=2),
                                                           "limit": None if r["limit"] is None else fmt_number(r["limit"], digits=2)}
                                                          for r in it["gate_ladder"]],
                    "bar_close": it["bar_close"] if it else None})
    intents = s.intents_visible()
    per_cycle: dict[str, int] = {}
    for x in intents:
        per_cycle[x["bar_close"]] = per_cycle.get(x["bar_close"], 0) + (x["outcome"] != "ENTERED")
    uni = activity_universe(s)
    dd_row = ladder[-1]
    return {
        "tier_banner": {"tier": tier, "name": TIER_NAMES[tier], "text": f"Paper session simulating {tier} {TIER_NAMES[tier]} rules. "
                        "The fund is at T0 until a tier is approved with the hardware key.",
                        "micro_notice": MICRO_NOTICE if tier == "T1" else None},
        "mode": "PAPER", "engine_state": "RUNNING",
        "nav": nav_fig, "hwm": F(s, "hwm", "High-water mark", st["hwm"], "DERIVED", unit="USD", **obs),
        "drawdown": dd_row["value"],
        "pnl": [{"period": k, "value": F(s, f"pnl-{k}", f"Net P&L {k}", v, "DERIVED", unit="USD", **obs), "limit_row": lr}
                for k, v, lr in [("today", pnl(0), "daily"), ("5d", pnl(5), "5d"), ("20d", pnl(20), "20d"), ("MTD", nav - mtd_base, None)]],
        "loss_ladder": ladder, "positions": positions, "why_not_trading": why,
        "next_rung": {"rung": dd_row["next_rung"], "consequence": dd_row["consequence"]},
        "incidents": incidents(s)["open"],
        "abstention_sparkline": {"series": [per_cycle[k] for k in sorted(per_cycle)], "class": "DERIVED",
                                 "label": "non-entered intents per cycle, last 30 days"},
        "pulse": {"admissible": F(s, "pulse-adm", "Admissible pairs", sum(1 for w in why if w["binding_gate"] not in LADDER_ORDER[:6]),
                                  "DERIVED", **obs),
                  "b_positive": F(s, "pulse-b", "Pairs with B > 0", sum(1 for x in s.series if (_fnum(s.result.signals[x.instrument_id]["B"][s.last_index()]) or 0) > 0),
                                  "DERIVED", **obs),
                  "T": [c["T"] | {"pair": c["pair"]} for c in uni["cards"]],
                  "cash": F(s, "pulse-cash", "Cash", 1 - gross / nav, "DERIVED", unit="%", digits=1, **obs),
                  "cost_gate_passes": F(s, "pulse-cg", "Cost-gate passes", sum(1 for x in s.series if (cost_r(s, x.instrument_id, s.last_index()) or 9)
                                                                              <= s.policy["cost"]["cost_R_max"]), "DERIVED", **obs)},
        "start_stop": {"control": "Governance", "text": "START/STOP is a kill switch: it needs a hardware-key approval in Governance."},
        "fixture": True}


# ---------------------------------------------------------------- markets
def markets(s: PaperSession, pair: str, bars: int = 360, tf: str = "4h") -> dict[str, Any]:
    base = activity_pair(s, pair, bars)
    out = {**base, "timeframe": tf, "timeframes": {"4h": "dominant", "1d": "aggregated server-side", "1h": "MISSING: no 1h fixture data"}}
    if tf == "1d":
        b = base["bars"]
        n = len(b["c"]) // BPD * BPD
        k0 = len(b["c"]) - n
        out["bars"] = {"time": b["time"][k0 + BPD - 1::BPD], "o": b["o"][k0::BPD], "h": [max(b["h"][j:j + BPD]) for j in range(k0, len(b["h"]), BPD)],
                       "l": [min(b["l"][j:j + BPD]) for j in range(k0, len(b["l"]), BPD)], "c": b["c"][k0 + BPD - 1::BPD], "gaps": 0}
    out["venue"] = {"venue": "paper (simulated Kraken spot)", "data_age": F(s, "mkt-age", "Data age", "FIXTURE", "OBSERVED",
                                                                            observed_at=s.bar_close(s.last_index()), ttl_s=BAR_TTL_S)}
    out["funding"] = {"interval": None, "text": "Spot: no funding (structural zero). Perp funding shows the venue's own interval "
                      "from its capability snapshot when a perp venue is connected."}
    out["compare"] = [pair_name(x.instrument_id) for x in s.series]
    return out


# ---------------------------------------------------------------- strategy + router
def _harness(s: PaperSession) -> dict | None:
    return s.harness


def validation(s: PaperSession) -> dict[str, Any]:
    h = _harness(s)
    rows = []
    recs = []
    got = {v["step"]: v for v in (h or {}).get("verdicts", [])}
    for step in range(1, 9):
        v = got.get(step)
        if v is None:
            rec = record_verdict(step, "A_long", "NOT_RUN", None, "—", None, (None, None))
        else:
            try:
                rec = record_verdict(step, "A_long", v["verdict"], v.get("run_id"), v.get("metric", ""), v.get("value"),
                                     tuple(v.get("ci") or (None, None)))
            except VerdictRejected as e:
                rec = record_verdict(step, "A_long", "FAIL", v.get("run_id"), v.get("metric", ""), v.get("value"),
                                     tuple(v.get("ci") or (None, None)), details={"rejected": str(e)})
        recs.append(rec)
        ci = list(rec.ci)
        rows.append({"step": step, "verdict": F(s, f"step-{step}", f"Step {step}", rec.verdict, "REPORTED" if h else "DERIVED",
                                                kind="verdict", fixture=True, extra={"run_id": rec.run_id, "ci": ci, "step": step}),
                     "run_id": rec.run_id, "metric": rec.metric,
                     "value": None if rec.value is None else fmt_number(rec.value, digits=3),
                     "ci": None if ci[0] is None and ci[1] is None else " to ".join("—" if x is None else fmt_number(x, digits=2) for x in ci),
                     "falsification": STEP_FALSIFICATION[step], "gates_shadow": step != 6})
    from research.harness.verdicts import SHADOW_STEPS, allowed_mode
    passed = sum(1 for r in recs if r.verdict == "PASS" and r.step in SHADOW_STEPS)
    maturity = ["Specified", "Implemented-unverified", "Run-verified", "§9.3 steps PASS", "Shadow 90d", "Canary 60d", "Validated/signed"]
    cur = 2 if h else 1
    return {"sleeves": [{"sleeve": "A_long", "maturity": maturity, "current": maturity[cur], "steps": rows,
                         "distance": f"{passed} of {len(SHADOW_STEPS)} SHADOW steps PASS", "allowed_mode": allowed_mode(recs)},
                        {"sleeve": "B_short", "maturity": maturity, "current": maturity[1], "steps": [], "distance": "not run",
                         "allowed_mode": "PAPER"},
                        {"sleeve": "B_long", "maturity": maturity, "current": maturity[1], "steps": [], "distance": "not run",
                         "allowed_mode": "PAPER"}],
            "mode_ladder": ["PAPER", "SHADOW", "CANARY", "LIVE"], "source": "harness run records" if h else "no run records on file",
            "promotion": {"enabled": False, "reason": "promotion needs every prior step PASS"}, "fixture": True}


def strategy(s: PaperSession) -> dict[str, Any]:
    i = s.last_index()
    tier = active_tier(s)
    val = validation(s)["sleeves"][0]
    pos, gross, nav = _book(s)
    open_risk = math.fsum(p["risk"] or 0 for p in pos) / nav
    close = s.bar_close(i)
    obs = {"observed_at": close, "ttl_s": BAR_TTL_S}
    sleeves = [{"sleeve": "A_long", "status": f"paper · simulated at {tier}", "validation": val["distance"],
                "risk_used": F(s, "sl-A-risk", "Risk budget used", open_risk / s.policy["sizing"]["cluster"]["open_risk_cap"], "DERIVED",
                               unit="%", digits=0, **obs)},
               {"sleeve": "B_short", "status": "paper — needs T3", "validation": "not run", "risk_used": None},
               {"sleeve": "B_long", "status": "paper — needs T4", "validation": "not run", "risk_used": None},
               {"sleeve": "B_xs", "status": "research", "validation": "not run", "risk_used": None}]
    ranking = []
    for x in s.series:
        k = x.instrument_id
        sig = s.result.signals[k]
        lo, hi = t_band(x.c, sig, s.policy, np.array([i]))
        rc = regime_claim_at(s, k)
        T = _fnum(sig["T"][i])
        cr = cost_r(s, k, i)
        ranking.append({"pair": pair_name(k), "T": F(s, f"rank-T-{k}", "T", T, "ESTIMATED", interval=(float(lo[0]), float(hi[0])) if T is not None else None,
                                                     digits=2, **obs),
                        "decomposition": {c: F(s, f"rank-{c}-{k}", c, _fnum(sig[c][i]), "DERIVED", digits=2, **obs) for c in ("B", "M", "Z")},
                        "m_regime": rc["payload"]["m_regime"] if rc else None, "m_claim_id": rc["claim_id"] if rc else None,
                        "cost_R": {"kraken (paper sim)": F(s, f"cg-{k}", "cost_R", cr, "DERIVED", digits=3, **obs),
                                   "binance": "reference price only (read-only)", "bybit": "reference price only (read-only)"}})
    ranking.sort(key=lambda r: -(r["T"]["value"] if r["T"]["value"] is not None else -9))
    closed, _o = s.trades_visible()
    n = len(closed)
    wins = sum(1 for t in closed if t.pnl > 0)
    p = wins / n if n else None
    avg_cr = float(np.mean([c for c in (cost_r(s, x.instrument_id, i) for x in s.series) if c is not None]))
    from research.harness.stats import n_eff_for_test
    ne = n_eff_for_test(n, len(s.series), test="single", rho=0.3, rho_kind="rho_return") if n else 0.0
    w_star = ((1 - p) + avg_cr) / p if p else None
    exp = {"p": F(s, "exp-p", "Win rate p", p, "ESTIMATED", unit="%", digits=1, n_eff=ne,
                  interval=_wilson(wins, n) if n else None, extra={"n_raw": n}),
           "W_star": F(s, "exp-w", "Required win/loss W* = ((1−p)+cost_R)/p", w_star, "DERIVED", digits=2),
           "avg_R": F(s, "exp-R", "Average R", float(np.mean([t.R for t in closed])) if closed else None, "ESTIMATED",
                      interval=_mean_ci([t.R for t in closed]) if n > 2 else None, digits=2, n_eff=ne),
           "n_eff_note": f"n_eff {fmt_number(ne, digits=1)} after deflation (rho_return 0.3, ASSUMED) vs {n} matured episodes"}
    abl = None
    if s.harness:
        v3 = next((v for v in s.harness["verdicts"] if v["step"] == 3), None)
        abl = {"verdict": v3["verdict"], "run_id": v3["run_id"]} if v3 else None
    params = []
    for sec in ("signal", "stops", "sizing", "cost"):
        for k, v in s.policy[sec].items():
            if isinstance(v, (int, float, str, list)) and not isinstance(v, bool):
                params.append({"param": f"{sec}.{k}", "value": str(v), "class": "D (policy)", "editable": False})
    return {"sleeves": sleeves, "ranking": ranking, "expectancy": exp, "ablation": abl or {"verdict": "NOT_RUN", "run_id": None},
            "parameters": params, "propose": {"route": "Governance → hypothesis registry", "text": "Parameter changes start as a hypothesis."},
            "rebalance_band": {"status": "within band", "class": "DERIVED"}, "fixture": True}


def _wilson(k: int, n: int, z: float = 1.645) -> tuple[float, float]:
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (c - h, c + h)


def _mean_ci(x: list[float], z: float = 1.645) -> tuple[float, float]:
    a = np.asarray(x, float)
    m, se = float(a.mean()), float(a.std(ddof=1) / math.sqrt(len(a)))
    return (m - z * se, m + z * se)


def router(s: PaperSession) -> dict[str, Any]:
    today = s.clock.date()
    ok = bool(s.evidence["passed"])
    hist = [(datetime.fromtimestamp(t, tz=timezone.utc).date(), v, ok) for t, v in _daily_navs(s)]
    ev = TierEvidence(nav_history=hist, drawdown=loss_state(s)["drawdown"])
    el = eligibility(s.policy, ev, today)
    ladder = []
    for t in TIERS[1:]:
        gates = el.gates[t]
        ladder.append({"tier": t, "name": TIER_NAMES[t], "nav_floor": fmt_number(s.policy["tiers"][t]["nav_up"], "USD", 0),
                       "gates": [{"gate": g["gate"], "passed": g["passed"], "detail": g.get("detail", "")} for g in gates],
                       "binding": next((g["gate"] for g in gates if not g["passed"]), None)})
    return {"ladder": ladder, "user_selected": "T0", "eligible": el.eligible_tier, "active": "T0", "paper_tier": active_tier(s),
            "derivatives_record": {"status": "none on file", "expiry": None},
            "allocator": {"state": f"inactive at {active_tier(s)}", "dwell_days": s.policy["allocator"]["min_dwell_days"],
                          "changes_left_this_year": s.policy["allocator"]["max_changes_per_year"]},
            "request_tier": {"route": "Governance", "text": "Request tier creates a strategy_request; an upgrade needs a signed approval. "
                             "A refused request shows its binding gate."},
            "fixture": True}


# ---------------------------------------------------------------- probability
def probability(s: PaperSession, pair: str) -> dict[str, Any]:
    inst = resolve_pair(s, pair)
    rc = regime_claim_at(s, inst)
    if rc is None:
        return {"pair": pair_name(inst), "abstain": "INSUFFICIENT_HISTORY", "fixture": True}
    p = rc["payload"]
    counts = np.array(p["counts_decayed"])
    P = (counts + 1) / (counts + 1).sum(axis=1, keepdims=True)
    k_star = information_horizon(P)
    w, v = np.linalg.eig(P.T)
    pi = np.real(v[:, np.argmin(np.abs(w - 1))])
    pi = pi / pi.sum()
    close = s.bar_close(s.last_index())
    obs = {"observed_at": close, "ttl_s": BAR_TTL_S}
    r = STATES.index(p["state"])
    nxt = [F(s, f"pn-{inst}-{st}", f"P(next = {st})", q["mean"], "ESTIMATED", kind="probability", interval=(q["lo90"], q["hi90"]),
             n_eff=p["ESS"][r], unit="%", digits=0, extra={"counts_decayed": p["counts_decayed"]}, **obs) for st, q in p["P_next"].items()]
    auth = p["authority"]
    m = F(s, f"m-{inst}", "m_regime", p["m_regime"], "DERIVED", kind="m_regime", digits=2, **obs)
    return {"pair": pair_name(inst), "claim_id": rc["claim_id"], "state": p["state"], "next": nxt,
            "matrix": {"states": list(STATES), "mean": [[round(float(x), 4) for x in row] for row in P],
                       "ess": [round(x, 1) for x in p["ESS"]], "counts_decayed": p["counts_decayed"]},
            "stationary": {st: round(float(x), 4) for st, x in zip(STATES, pi, strict=True)},
            "k_star": k_star, "k_slider": {"max": k_star, "disabled_beyond": k_star},
            "m": m, "m_note": "computed, not applied" if auth == "T0" else "applied to size",
            "binding_reasons": p["binding_reasons"], "authority": auth,
            "authority_badge": "T0 computed, not applied" if auth == "T0" else "T1 applied",
            "calibration": {"ece": None, "authoritative": False, "text": "ECE not yet measured: calibration non-authoritative"},
            "contexts": [{"id": f"M{j}", "eligible": False, "weight": 0.0} for j in range(1, 8)],
            "invalidates": "ESS below 30, a 90% interval wider than 0.50, ECE ≥ 0.08, a stress state, or stale bars",
            "fixture": True}


# ---------------------------------------------------------------- P&L
def pnl(s: PaperSession) -> dict[str, Any]:
    ev = s.evidence
    closed, _o = s.trades_visible()
    et, eq = s.equity_visible()
    close = s.bar_close(s.last_index())
    obs = {"observed_at": close, "ttl_s": BAR_TTL_S}
    fees = math.fsum(t.fees for t in closed)
    months = max(1.0, (et[-1] - et[0]) / (30 * 86400)) if len(et) > 1 else 1.0
    opex = s.policy["operating_costs_usd_month"] * months
    notional = math.fsum(t.qty * t.entry_px + t.qty * t.exit_px for t in closed)
    spread = notional * s.costs.half_spread
    slip = notional * s.costs.slippage_q75
    stack = [("Fees", fees, "DERIVED"), ("Spread (modelled)", spread, "ESTIMATED"), ("Slippage q75 (modelled)", slip, "ESTIMATED"),
             ("Fixed operating cost", opex, "ASSUMED")]
    uf = UnitFund()
    for t, v in zip(et[::6], eq[::6], strict=True):
        ts = datetime.fromtimestamp(int(t), tz=timezone.utc)
        if uf.units == 0:
            uf.deposit(ts, v, 0)
        else:
            uf.mark(ts, v)
    comps = ev["components"]
    net_ledger = float(ev["ledger_net"])
    return {
        "net": [F(s, "pnl-net", "Net P&L (whole replay)", net_ledger, "DERIVED", unit="USD", lineage=["ledger"],
                  verification=nav_verification(s), **obs)],
        "equity": {"time": [datetime.fromtimestamp(int(t), tz=timezone.utc).isoformat() for t in et[::6]],
                   "nav": _series_list(eq[::6], 2), "class": "DERIVED",
                   "rungs": [{"rung": r["rung"], "level": r["level"]} for r in loss_ladder(s)[-1]["rungs"]]},
        "cost_stack": [{"item": k, "value": F(s, f"cost-{k}", k, v, c, unit="USD",
                                              interval=(v * 0.5, v * 1.5) if c == "ESTIMATED" else None,
                                              owner="principal" if c == "ASSUMED" else None,
                                              review_by="2026-12-31" if c == "ASSUMED" else None)} for k, v, c in stack],
        "cost_hurdle": F(s, "hurdle", "Cost hurdle (sum of stack)", math.fsum(v for _, v, _ in stack), "DERIVED", unit="USD"),
        "attribution": {"components": {k: F(s, f"att-{k}", k, float(v), "DERIVED", unit="USD") for k, v in comps.items()},
                        "process": {k: F(s, f"proc-{k}", k, float(v), "DERIVED", unit="USD") for k, v in ev.get("process", {}).items()},
                        "sums_to_net": ev["attribution_net"] == ev["ledger_net"], "net": ev["ledger_net"],
                        "funding_row": F(s, "att-funding", "Funding (Strategy A)", 0.0, "DERIVED", unit="USD",
                                         extra={"structural_zero": True})},
        "ledger": {"entries": ev["ledger_entries"], "chain_ok": ev["ledger_chain_ok"], "head": ev["ledger_head"],
                   "incident": None if ev["ledger_chain_ok"] else "LEDGER_CHAIN_BREAK"},
        "tax": {"jurisdiction": "UK", "rule_version": F(s, "tax-rule", "Rule version", "UK CGT share matching", "ASSUMED",
                                                          owner="principal", review_by="2026-12-31"),
                "report_currency": "GBP (tax engine only)", "note": "Not tax advice. Accountant sign-off before LIVE."},
        "twr": F(s, "twr", "TWR", float(uf.twr), "DERIVED", unit="%"),
        "irr": F(s, "irr", "IRR (annualised)", uf.irr(datetime.fromtimestamp(int(et[-1]), tz=timezone.utc)) if len(et) > 1 else None,
                 "DERIVED", unit="%"),
        "profit_allocation": {"control": "Governance", "text": "Profit allocation changes are Governance proposals."},
        "reports": [], "fixture": True}


# ---------------------------------------------------------------- outcomes
def outcomes(s: PaperSession) -> dict[str, Any]:
    r = s.result.daily_returns
    cut = int(s.clock.timestamp())
    r = r[s.result.daily_time <= cut]
    rng = np.random.default_rng(17)
    nav = loss_state(s)["nav"]

    def fan(x: np.ndarray) -> dict[str, Any]:
        paths = np.cumprod(1 + rng.choice(x, size=(2000, 365), replace=True), axis=1)
        q = np.quantile(paths, [0.05, 0.25, 0.5, 0.75, 0.95], axis=0)[:, ::7]
        dd = 1 - paths / np.maximum.accumulate(np.maximum(paths, 1.0), axis=1)
        return {"quantiles": [[round(float(v), 5) for v in row] for row in q], "p_loss": float(np.mean(paths[:, -1] < 1)),
                "p_ruin": float(np.mean(dd.max(axis=1) >= s.policy["risk"]["ladder"]["dd"]["terminate"])),
                "dd_at_risk_usd": float(np.quantile(dd.max(axis=1), 0.95) * nav)}
    base = fan(r) if len(r) > 30 else None
    null = fan(r - r.mean()) if len(r) > 30 else None

    def figs(tag: str, f: dict | None) -> dict | None:
        if f is None:
            return None
        return {**f, "p_loss": F(s, f"{tag}-ploss", "P(loss, 1y)", f["p_loss"], "HYPOTHETICAL", unit="%", digits=1),
                "p_ruin": F(s, f"{tag}-pruin", "P(DD ≥ 20%, 1y)", f["p_ruin"], "HYPOTHETICAL", unit="%", digits=1),
                "dd_at_risk_usd": F(s, f"{tag}-ddar", "DD at risk (95%)", f["dd_at_risk_usd"], "HYPOTHETICAL", unit="USD", digits=0)}
    stress = run_battery(s.policy, s.policy_hash, now=s.clock)
    wf = None
    if s.harness:
        v2 = next((v for v in s.harness["verdicts"] if v["step"] == 2), None)
        wf = {"run_id": v2["run_id"] if v2 else None, "details": (v2 or {}).get("details")}
    return {"projection": figs("base", base), "null_edge": figs("null", null), "null_edge_hideable": False,
            "projection_note": "Bootstrap of FIXTURE daily returns: HYPOTHETICAL, never added to P&L.",
            "stress": {"run_id": stress["run_id"], "verdict": stress["verdict"],
                       "scenarios": [{**x, "loss_display": fmt_number(x["loss"], "%", 1)} for x in stress["scenarios"]]},
            "walk_forward": wf, "counterfactual": {"class": "HYPOTHESIS", "promotable_here": False,
                                                   "text": "Counterfactuals need a run_id and a registry debit; there is no route to live."},
            "abstentions": {"class": "HYPOTHETICAL", "added_to_pnl": False}, "fixture": True}


def playback(s: PaperSession, pair: str, cursor: datetime) -> dict[str, Any]:
    """Episode playback: the masked session answers; nothing after the cursor is in the response."""
    m = s.mask(cursor)
    return {"cursor": cursor.isoformat(), "pair": activity_pair(m, pair, WINDOW_BARS), "fund": {"nav": fund_room(m)["nav"]},
            "masked": True}


# ---------------------------------------------------------------- data, bots, incidents, governance, settings
def data(s: PaperSession) -> dict[str, Any]:
    close = s.bar_close(s.last_index())
    feeds = []
    for x in s.series:
        feeds.append({"venue": "paper", "dataset": f"{pair_name(x.instrument_id)} ohlcv 4h",
                      "freshness": F(s, f"feed-{x.instrument_id}", "Last bar", close.isoformat(), "OBSERVED", observed_at=close, ttl_s=BAR_TTL_S),
                      "gaps": int(np.sum(np.diff(x.open_time) != H4)), "quarantine": 0, "certified": False, "clock_skew_ms": 0})
    for v in ("binance-spot (read-only)", "bybit-v5 (read-only)", "kraken-spot"):
        feeds.append({"venue": v, "dataset": "not connected", "freshness": None, "gaps": None, "quarantine": None,
                      "certified": False, "clock_skew_ms": None})
    n = len(s.series[0].c)
    return {"feeds": feeds,
            "builds": [{"dataset": "fixture universe", "hash": s.data_hash(), "quality_report": None,
                        "certifiable": False, "reason": "no quality report: a build without one cannot be certified"}],
            "coverage": {"bars": n, "from": datetime.fromtimestamp(int(s.series[0].open_time[0]), tz=timezone.utc).isoformat(),
                         "days": n // BPD, "consistent": n == (int(s.series[0].open_time[-1] - s.series[0].open_time[0]) // H4 + 1)},
            "lineage": {"root": "engine.data.fixtures.fixture_bars (seeded generator)", "zero_llm_ancestry": True},
            "drift": {"status": "not run", "reason": "needs certified history"},
            "admissibility": [{"pair": pair_name(x.instrument_id), "admissible": _fnum(s.result.signals[x.instrument_id]["T"][s.last_index()]) is not None}
                              for x in s.series],
            "fixture": True}


def bots(s: PaperSession, light: bool = False) -> dict[str, Any]:
    tier = active_tier(s)
    auth = s.policy["regime"]["authority"]
    matrix = [
        {"agent": "Data sentinel", "authority": "T3", "may_emit": "freshness, quarantine", "never": "originate orders"},
        {"agent": "Signal engine", "authority": "T1", "may_emit": "signal_claim", "never": "size, execute"},
        {"agent": "Regime (CCMRM)", "authority": auth, "may_emit": "regime_claim, m ≤ 1", "never": "write limits, originate"},
        {"agent": "Strategy router", "authority": "T3", "may_emit": "tier_eligibility_claim, active tier", "never": "upgrade a tier"},
        {"agent": "Allocator", "authority": "T2", "may_emit": "risk budgets", "never": "exceed exposure map, raise intraday"},
        {"agent": "Risk authority", "authority": "T3", "may_emit": "limit state, kill", "never": "raise its own limits"},
        {"agent": "Verifier", "authority": "T0", "may_emit": "reconciliation", "never": "trade"},
        {"agent": "Reporter (A11)", "authority": "INFORM", "may_emit": "prose with citations", "never": "any claim, intent, figure not in the bundle"},
    ]
    modules = [{"module": m, "param_hash": s.policy_hash, "conflicted": False} for m in ("signal", "sizing", "stops", "regime", "router")]
    for m in modules:
        m["conflicted"] = m["param_hash"] != s.policy_hash
    if light:
        return {"modules": modules}
    registry = [{"model": "CCMRM", "version": "0.1.0", "authority": auth, "state_rule": "A-REGIME-STATES (ASSUMED)"},
                {"model": "Trend ensemble T", "version": "0.1.0", "authority": "T1", "state_rule": "policy.signal"}]
    return {"authority_matrix": matrix, "model_registry": registry, "modules": modules,
            "emissions": emissions_projection(s),
            "calibration": [{"model": "CCMRM", "ece": None, "authoritative": False, "text": "not yet measured"}],
            "verifier": {"heartbeat": "ok" if s.stream_alive else "lost", "last_kill_drill": {"passed": s.drill.get("passed"),
                                                                                                "run_id": s.drill.get("run_id")}},
            "hypothesis_budget": _hypothesis_budget(s), "instruction_log": list(s.reporter_log[-50:]),
            "apply_control": False, "fixture": True, "inactive": [f"Allocator inactive at {tier}"]}


def _hypothesis_budget(s: PaperSession) -> dict[str, Any]:
    from research.registry.registry import budget_used, load
    items = load()
    year = s.clock.year
    return {"used": budget_used(items, year), "budget": s.policy["governance"].get("hypothesis_budget_per_year"), "year": year,
            "registered": len(items)}


def incidents(s: PaperSession) -> dict[str, Any]:
    inc = s.incidents.items
    rows = [{"code": i.code, "severity": i.severity, "scope": i.scope, "detail": i.detail, "open": i.open,
             "owner": "principal", "deadline": None, "what_system_did": "entries blocked in scope" if i.open else "resolved",
             "resolution": i.resolution or None, "runbook": f"ops/runbooks/{i.code}.md"} for i in inc]
    return {"open": [r for r in rows if r["open"]], "resolved": [r for r in rows if not r["open"]], "dismiss_control": False,
            "resolution_rule": "Resolution requires a written rationale.", "deadman": [], "fixture": True}


def governance(s: PaperSession) -> dict[str, Any]:
    import json
    signers = json.loads((ROOT / "policy" / "signers" / "signers.json").read_text())
    assumed = yaml.safe_load((ROOT / "policy" / "assumed_register.yaml").read_text())["assumed"]
    from research.registry.registry import load
    return {"policy": {"version": s.policy_version, "hash": s.policy_hash, "active": False,
                       "status": "not activated: no enrolled hardware key has signed it"},
            "signers": [{"signer_id": x["signer_id"], "keys_enrolled": len(x["keys"])} for x in signers["signers"]],
            "proposals": [], "approvals": [], "tier_requests": [], "access_records": [],
            "assumed": [F(s, a["id"], a["id"], a["value"], "ASSUMED", owner=a["owner"], review_by=a["review_by"], fixture=False,
                          source=a.get("source", "")) for a in assumed],
            "hypotheses": [{"id": h["id"], "status": h.get("status"), "run_ids": h.get("run_ids", []), "class": h.get("class")} for h in load()],
            "mode_ladder": ["PAPER", "SHADOW", "CANARY", "LIVE"], "external_review": "ASSUMED: before CANARY",
            "on_call": "not required in PAPER", "coherence_rule": "A signature cannot activate an incoherent policy.", "fixture": True}


def exchanges(s: PaperSession) -> dict[str, Any]:
    reg = ConnectorRegistry.shipped()
    types = [{"connector_type": c.connector_type, "venue": c.venue, "products": list(c.products), "environments": list(c.environments),
              "trade_capable": c.trade_capable, "trust_cap": c.trust_cap, "dead_man": c.dead_man_switch} for c in reg.types.values()]
    return {"connector_types": types, "instances": [],
            "paper": {"venue": "sim", "state": "PAPER_ENABLED", "note": "Paper sessions use the simulated venue; no key is read."},
            "rules": ["Exchanges and API keys are added here, never in code.",
                      "Enabling trading on a venue opens a Governance proposal instead of acting.",
                      "Binance and Bybit stay read-only until the venue confirms UK use in writing.",
                      "No VPN or location masking (INV-43)."],
            "add_exchange": {"route": "Settings → Exchanges → Add (creates a DRAFT instance)", "writes_here": False}, "fixture": True}


def settings(s: PaperSession) -> dict[str, Any]:
    return {"profile": {"principal": "principal", "mfa": "hardware key (not yet enrolled)", "sessions": []},
            "notifications": [{"class": "S1", "route": "push + SMS, dead-man"}, {"class": "D2", "route": "push"},
                              {"class": "H1", "route": "email"}, {"class": "info", "route": "in-app"}],
            "display": {"time": "UTC primary", "currency": "USD (fixed)", "themes": ["light", "dark"], "density": ["comfortable", "compact"]},
            "exchanges": exchanges(s), "stated_limits": {"hard_drawdown": fmt_number(s.policy["risk"]["ladder"]["dd"]["terminate"], "%", 0)},
            "trading_parameters_editable": False, "fixture": True}


def venues(s: PaperSession) -> dict[str, Any]:
    return {"venues": exchanges(s)["connector_types"], "instances": [], "fixture": True}


def whole_screen(s: PaperSession, name: str) -> Mapping[str, Any]:
    return SCREENS[name](s)


SCREENS = {"fund-room": fund_room, "strategy": strategy, "strategy/router": router, "pnl": pnl, "outcomes": outcomes, "data": data,
           "bots": bots, "risk": risk, "incidents": incidents, "venues": venues, "governance": governance, "validation": validation,
           "settings/exchanges": exchanges, "settings": settings}
