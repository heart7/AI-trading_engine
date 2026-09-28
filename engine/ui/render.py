"""Class-bound rendering contract (spec §16.6). Every figure the UI shows passes through here.

The BFF turns each figure into a claim (`figure()`), then `render()` attaches the only styling the client
may use. The browser draws what it is told; it never picks a style or computes a material figure (§15.3).
`audit()` checks a rendered figure against the rules and is used both on BFF output and on DOM snapshots,
so a renderer that draws FIXTURE data in observed style fails a test (INV-32).

Rules enforced (each has a negative test in tests/negative/test_p5_invariants.py):
- OBSERVED solid; DERIVED solid + lineage glyph; ESTIMATED always banded, never a bare point;
  ASSUMED is a chip with owner + review date; REPORTED hatched and muted, never exportable as evidence;
  HYPOTHESIS outlined dashed.
- FIXTURE (certified: false) is hatched and labelled on every figure (INV-32).
- STALE past TTL greys the figure, shows the last-good time and cannot be hidden.
- CONFLICTED raises a persistent top-bar chip; ABSTAIN carries its binding reason; gaps stay gaps.
- Sample-size warning is non-dismissible when n_eff < floor (policy.ui.n_eff_warning_floor).
- m_regime > 1 cannot render; a probability without counts_decayed cannot render; a live-only Sharpe
  cannot render as evidence of edge; a PASS on a CI lower bound <= 0 or without a run_id cannot render.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

CLASSES = ("OBSERVED", "DERIVED", "ESTIMATED", "ASSUMED", "REPORTED", "HYPOTHESIS", "HYPOTHETICAL")
STATES = (None, "STALE", "CONFLICTED", "ABSTAIN", "MISSING", "INTERPOLATED")
BASE_STYLE = {"OBSERVED": "solid", "DERIVED": "solid-lineage", "ESTIMATED": "banded", "ASSUMED": "chip",
              "REPORTED": "hatched-muted", "HYPOTHESIS": "outlined-dashed", "HYPOTHETICAL": "outlined-dashed"}
N_EFF_FLOOR_DEFAULT = 30


class RenderViolation(Exception):
    """A figure would render in a way §16.6 forbids."""

    def __init__(self, rule: str, detail: str = ""):
        self.rule = rule
        super().__init__(f"{rule}: {detail}" if detail else rule)


def fmt_number(v: float | int | None, unit: str = "", digits: int | None = None) -> str:
    """Server-side formatting: the client prints `display` and never formats or computes a figure."""
    if v is None:
        return "—"
    if unit == "%":
        return f"{v * 100:.{2 if digits is None else digits}f}%"
    if unit == "USD":
        return f"${v:,.{2 if digits is None else digits}f}"
    if unit == "px":
        a = abs(v)
        return f"{v:,.0f}" if a >= 1000 else f"{v:.2f}" if a >= 10 else f"{v:.4f}"
    if isinstance(v, int) and digits is None:
        return f"{v:,d}"
    return f"{v:.{2 if digits is None else digits}f}" + (f" {unit}" if unit else "")


def figure(fid: str, label: str, value: Any, cls: str, *, unit: str = "", digits: int | None = None,
           interval: tuple[float, float] | None = None, n_eff: float | None = None, fixture: bool = True,
           observed_at: datetime | None = None, now: datetime | None = None, ttl_s: int | None = None,
           source: str = "", lineage: Iterable[str] = (), owner: str | None = None, review_by: str | None = None,
           state: str | None = None, reason: str | None = None, verification: Mapping | None = None,
           kind: str | None = None, extra: Mapping | None = None) -> dict[str, Any]:
    """A figure as a claim: class, interval, n_eff and data age travel with the value (§15.3)."""
    if cls not in CLASSES:
        raise RenderViolation("UNKNOWN_CLASS", cls)
    age = None
    if observed_at is not None and now is not None:
        age = max(0, int((now - observed_at).total_seconds()))
    return {"id": fid, "label": label, "value": value, "unit": unit, "digits": digits, "class": cls,
            "interval": list(interval) if interval is not None else None, "n_eff": n_eff, "fixture": fixture,
            "certified": not fixture, "age_s": age, "ttl_s": ttl_s,
            "last_good": observed_at.isoformat() if observed_at is not None else None, "source": source,
            "lineage": list(lineage), "owner": owner, "review_by": review_by, "state": state, "reason": reason,
            "verification": dict(verification) if verification else None, "kind": kind, **dict(extra or {})}


def _check_kind(f: Mapping[str, Any]) -> None:
    k = f.get("kind")
    if k == "m_regime":
        v = f.get("value")
        if v is not None and float(v) > 1.0:
            raise RenderViolation("M_REGIME_ABOVE_ONE", f"{f['id']}: m = {v}")
    if k == "probability" and not f.get("counts_decayed"):
        raise RenderViolation("PROBABILITY_WITHOUT_COUNTS", f["id"])
    if k == "evidence_sharpe" and f.get("source_kind") == "live":
        raise RenderViolation("LIVE_SHARPE_AS_EVIDENCE", "a live horizon is not a test of edge (INV-20)")
    if k == "verdict":
        if f.get("value") == "PASS" and not f.get("run_id"):
            raise RenderViolation("PASS_WITHOUT_RUN_ID", f["id"])
        lb = (f.get("ci") or [None])[0]
        if f.get("value") == "PASS" and f.get("step") == 2 and not (lb is not None and lb > 0):
            raise RenderViolation("PASS_ON_NONPOSITIVE_CI", f"{f['id']}: lower bound {lb}")
    if k == "stop_series":
        pts = [p for p in f.get("value") or [] if p is not None]
        side = f.get("side", 1)
        if any((b - a) * side < -1e-12 for a, b in zip(pts, pts[1:], strict=False)):
            raise RenderViolation("STOP_MOVED_AGAINST_POSITION", f["id"])


def render(f: Mapping[str, Any], *, n_eff_floor: int = N_EFF_FLOOR_DEFAULT, hide: bool = False) -> dict[str, Any]:
    """Attach the rendering the client must use. Raises RenderViolation where §16.6 forbids the figure."""
    cls = f["class"]
    _check_kind(f)
    if cls == "ESTIMATED" and f.get("interval") is None and f.get("value") is not None:
        raise RenderViolation("ESTIMATE_WITHOUT_BAND", f"{f['id']} would render as a bare point")
    if cls == "ASSUMED" and not (f.get("owner") and f.get("review_by")):
        raise RenderViolation("ASSUMED_WITHOUT_OWNER", f["id"])
    state = f.get("state")
    ttl, age = f.get("ttl_s"), f.get("age_s")
    if ttl is not None and age is not None and age > ttl:
        state = "STALE"
    if hide and state == "STALE":
        raise RenderViolation("STALE_HIDDEN", f"{f['id']}: a stale figure cannot be hidden")
    if state == "ABSTAIN" and not f.get("reason"):
        raise RenderViolation("ABSTAIN_WITHOUT_REASON", f["id"])
    style = [BASE_STYLE[cls]]
    chips: list[dict[str, str]] = []
    if f.get("fixture"):
        style.append("hatched")
        chips.append({"kind": "FIXTURE", "text": "FIXTURE · certified: false"})
    if cls == "ASSUMED":
        chips.append({"kind": "ASSUMED", "text": f"ASSUMED · {f['owner']} · review {f['review_by']}"})
    if state == "STALE":
        style.append("stale")
        chips.append({"kind": "STALE", "text": f"STALE · last good {f.get('last_good') or 'unknown'}"})
    elif state:
        style.append(state.lower())
    warnings = []
    n = f.get("n_eff")
    if n is not None and n < n_eff_floor:
        warnings.append({"kind": "SAMPLE_SIZE", "dismissible": False,
                         "text": f"n_eff {fmt_number(float(n), digits=1)} < {n_eff_floor}: not enough evidence"})
    value, unit, digits = f.get("value"), f.get("unit", ""), f.get("digits")
    if state == "MISSING":
        display = "gap"
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        display = fmt_number(value, unit, digits)
    elif value is None:
        display = "—"
    else:
        display = str(value)
    band = None
    if f.get("interval") is not None:
        lo, hi = f["interval"]
        band = {"lo": lo, "hi": hi, "display": f"{fmt_number(lo, unit, digits)} to {fmt_number(hi, unit, digits)}"}
    out = dict(f)
    out["state"] = state
    out["render"] = {"style": style, "display": display, "band": band, "chips": chips, "warnings": warnings,
                     "lineage_glyph": cls == "DERIVED", "exportable_as_evidence": cls not in ("REPORTED", "HYPOTHETICAL")
                     and not f.get("fixture"), "hideable": state != "STALE"}
    audit(out, n_eff_floor=n_eff_floor)
    return out


def audit(r: Mapping[str, Any], *, n_eff_floor: int = N_EFF_FLOOR_DEFAULT) -> None:
    """Check a rendered figure (from the BFF or a DOM snapshot). Raises on any §16.6 violation."""
    spec = r.get("render")
    if not spec:
        raise RenderViolation("UNRENDERED", str(r.get("id")))
    style = spec["style"]
    if r.get("fixture") or r.get("certified") is False:
        if "hatched" not in style:
            raise RenderViolation("FIXTURE_NOT_HATCHED", f"{r.get('id')} drawn in {style[0]} style (INV-32)")
        if not any(c["kind"] == "FIXTURE" for c in spec["chips"]):
            raise RenderViolation("FIXTURE_NOT_LABELLED", str(r.get("id")))
    cls = r["class"]
    if style[0] != BASE_STYLE[cls]:
        raise RenderViolation("CLASS_STYLE_MISMATCH", f"{r.get('id')}: {cls} drawn as {style[0]}")
    if cls == "ESTIMATED" and r.get("value") is not None and not spec.get("band"):
        raise RenderViolation("ESTIMATE_WITHOUT_BAND", str(r.get("id")))
    if r.get("state") == "STALE" and ("stale" not in style or spec.get("hideable")):
        raise RenderViolation("STALE_NOT_SHOWN", str(r.get("id")))
    n = r.get("n_eff")
    if n is not None and n < n_eff_floor and not any(w["kind"] == "SAMPLE_SIZE" and not w["dismissible"]
                                                     for w in spec["warnings"]):
        raise RenderViolation("SAMPLE_SIZE_WARNING_MISSING", str(r.get("id")))
    if cls in ("REPORTED", "HYPOTHETICAL") and spec.get("exportable_as_evidence"):
        raise RenderViolation("REPORTED_EXPORTABLE", str(r.get("id")))
    _check_kind(r)


def rendered(*figs: Mapping[str, Any], n_eff_floor: int = N_EFF_FLOOR_DEFAULT) -> list[dict[str, Any]]:
    return [render(f, n_eff_floor=n_eff_floor) for f in figs]


def walk_figures(doc: Any) -> Iterable[Mapping[str, Any]]:
    """Every rendered figure inside a projection (dicts carrying a `render` key)."""
    if isinstance(doc, Mapping):
        if "render" in doc and "class" in doc:
            yield doc
        for v in doc.values():
            yield from walk_figures(v)
    elif isinstance(doc, list):
        for v in doc:
            yield from walk_figures(v)
