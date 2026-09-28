"""Strategy A position sizing (spec §5.5, INV-05, INV-10).

    size_raw = min(risk_size, kelly_cap, size_ES, size_liq, size_map)
    size     = size_raw * v * m_regime,   v = min(1, sigma* / sigma_book)

All sizes are USD notional. Spot gross <= 1.0 x NAV. Any input not FRESH, or mu_q <= 0, gives 0.

Interpretations (spec is silent, recorded here):
- kelly_cap = k * max(0, mu_q) / sigma^2 * NAV with daily mu_q and daily sigma of the instrument.
- size_ES uses a parametric normal ES97.5 (2.3378 sigma) on the 1-day book with the cluster stress
  correlation rho_stress between all large caps; the limit is es_mult * r_tier * NAV.
- size_liq = min(0.3 * depth within 50 bp, 10% of 0.3 * traded volume over the TTF ceiling).
- size_map = min(per_pair_notional_max * NAV, notional whose risk is 40% of the sleeve's open-risk cap).
- sigma_book is annualised: daily book vol x sqrt(365).
"""
from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

ES975_NORMAL = 2.3378  # phi(1.96) / 0.025
STRESSED_DEPTH = 0.3
PARTICIPATION = 0.10


@dataclass(frozen=True)
class Holding:
    notional: float  # USD, signed (+ long)
    sigma_daily: float


@dataclass(frozen=True)
class SizingInputs:
    nav: float
    r_tier: float
    d: float  # stop distance fraction
    sigma_daily: float  # this instrument
    mu_q_daily: float | None
    kelly_k: float
    es_mult: float
    rho_stress: float
    depth_50bp_usd: float
    volume_usd_per_min: float
    ttf_ceiling_min: float
    per_pair_notional_max: float
    per_instrument_risk_share_max: float
    open_risk_cap: float
    sigma_star_annual: float
    m_regime: float = 1.0
    regime_authority: str = "T0"
    regime_stress: bool = False
    stress_cut: float = 0.5
    book: Sequence[Holding] = ()
    inputs_fresh: bool = True
    gross_now: float = 0.0  # current spot gross notional, USD


@dataclass(frozen=True)
class SizingResult:
    size_usd: float
    risk_size: float
    kelly_cap: float
    size_ES: float
    size_liq: float
    size_map: float
    v: float
    m_applied: float
    binding_limit: str
    zero_reason: str | None


def _book_var(book: Sequence[Holding], extra: Holding | None, rho: float) -> float:
    xs = list(book) + ([extra] if extra else [])
    var = 0.0
    for i, a in enumerate(xs):
        for j, b in enumerate(xs):
            corr = 1.0 if i == j else rho
            var += a.notional * b.notional * a.sigma_daily * b.sigma_daily * corr
    return max(var, 0.0)


def size_es(inp: SizingInputs) -> float:
    """Largest long notional x with ES97.5(1d) of book+x <= es_mult * r_tier * NAV (closed form)."""
    limit = inp.es_mult * inp.r_tier * inp.nav / ES975_NORMAL  # max book sigma in USD
    s = inp.sigma_daily
    b = 2 * s * sum(h.notional * h.sigma_daily * inp.rho_stress for h in inp.book)
    c0 = _book_var(inp.book, None, inp.rho_stress) - limit ** 2
    a = s * s
    if c0 > 0:
        return 0.0
    disc = b * b - 4 * a * c0
    return max(0.0, (-b + math.sqrt(disc)) / (2 * a)) if a > 0 else math.inf


def compute_size(inp: SizingInputs) -> SizingResult:
    zero = None
    if not inp.inputs_fresh:
        zero = "INPUT_NOT_FRESH"
    elif inp.mu_q_daily is None:
        zero = "MU_Q_MISSING"
    elif inp.mu_q_daily <= 0:
        zero = "MU_Q_NONPOSITIVE"
    elif inp.d <= 0 or inp.sigma_daily <= 0:
        zero = "BAD_INPUT"
    risk_size = inp.r_tier * inp.nav / inp.d if inp.d > 0 else 0.0
    kelly = inp.kelly_k * max(0.0, inp.mu_q_daily or 0.0) / (inp.sigma_daily ** 2) * inp.nav if inp.sigma_daily > 0 else 0.0
    ses = size_es(inp) if inp.sigma_daily > 0 else 0.0
    sliq = min(STRESSED_DEPTH * inp.depth_50bp_usd,
               PARTICIPATION * STRESSED_DEPTH * inp.volume_usd_per_min * inp.ttf_ceiling_min)
    smap = min(inp.per_pair_notional_max * inp.nav,
               inp.per_instrument_risk_share_max * inp.open_risk_cap * inp.nav / inp.d if inp.d > 0 else 0.0)
    sgross = max(0.0, 1.0 * inp.nav - inp.gross_now)  # spot gross <= 1.0 x NAV
    cands = {"risk_size": risk_size, "kelly_cap": kelly, "size_ES": ses, "size_liq": sliq, "size_map": smap,
             "spot_gross": sgross}
    binding = min(cands, key=lambda k: cands[k])
    raw = cands[binding]
    # vol deflation on the book including this position, never > 1 (INV-05)
    book_sigma_ann = math.sqrt(_book_var(inp.book, Holding(raw, inp.sigma_daily), inp.rho_stress)) / inp.nav * math.sqrt(365)
    v = 1.0 if book_sigma_ann <= 0 else min(1.0, inp.sigma_star_annual / book_sigma_ann)
    # regime throttle and stress cut apply only at regime authority T1 (INV-10, §7.6)
    if inp.regime_authority == "T1":
        m = min(1.0, max(0.0, inp.m_regime))
        if inp.regime_stress:
            v = min(v, inp.stress_cut)
    else:
        m = 1.0
    size = 0.0 if zero else raw * v * m
    return SizingResult(size, risk_size, kelly, ses, sliq, smap, v, m, binding, zero)
