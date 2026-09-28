"""Funding model for Strategy B (spec §6.5, INV-17, INV-18, INV-24).

Funding is paid by longs to shorts when the rate is positive. For a book with side s (+1 long, -1 short) the
carry *paid* per event is s x rate x notional. Expectations are conditional on the book's signal state: the
unconditional mean has no code path into the cost gate. Until enough conditional observations exist, a book
assumes zero or adverse carry, never favourable. Hold is outcome-weighted: winners and losers are estimated
separately, because winners are held longer and so carry more of the funding.
"""
from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from engine.strategy_b.contracts import ContractSpec

MIN_CONDITIONAL_OBS = 200


class UnconditionalFunding(Exception):
    """Raised when anything tries to feed an unconditional funding mean into admission or the cost gate."""


@dataclass(frozen=True)
class FundingEstimate:
    instrument_id: str
    book: str  # B_long | B_short
    kind: str  # CONDITIONAL | UNCONDITIONAL (the latter exists only to be reported and refused)
    paid_per_day: float  # expected carry paid per day as a fraction of notional (positive = cost)
    sd_per_day: float
    n: int
    assumed: bool = False  # True when too few conditional observations: zero-or-adverse default applied


def conditional_estimate(instrument_id: str, book: str, rates: Sequence[float], in_state: Sequence[bool],
                         spec: ContractSpec) -> FundingEstimate:
    """Per-event rates and a mask of events that occurred while the book's signal state was active."""
    side = 1 if book == "B_long" else -1
    r = np.asarray(rates, float)[np.asarray(in_state, bool)]
    per_day = spec.funding_events_per_day()
    if len(r) < MIN_CONDITIONAL_OBS:
        mean = float(side * r.mean() * per_day) if len(r) else 0.0
        sd = float(r.std(ddof=1) * math.sqrt(per_day)) if len(r) > 1 else 0.0
        return FundingEstimate(instrument_id, book, "CONDITIONAL", max(0.0, mean), sd, len(r), assumed=True)
    paid = side * r
    return FundingEstimate(instrument_id, book, "CONDITIONAL", float(paid.mean() * per_day),
                           float(paid.std(ddof=1) * math.sqrt(per_day)), len(r))


def unconditional_estimate(instrument_id: str, book: str, rates: Sequence[float], spec: ContractSpec) -> FundingEstimate:
    side = 1 if book == "B_long" else -1
    r = np.asarray(rates, float)
    return FundingEstimate(instrument_id, book, "UNCONDITIONAL", float(side * r.mean() * spec.funding_events_per_day()),
                           float(r.std(ddof=1) * math.sqrt(spec.funding_events_per_day())), len(r))


@dataclass(frozen=True)
class OutcomeWeightedHold:
    p_win: float
    hold_win_days: float
    hold_loss_days: float

    @classmethod
    def from_episodes(cls, pnl: Sequence[float], hold_days: Sequence[float]) -> OutcomeWeightedHold:
        p, h = np.asarray(pnl, float), np.asarray(hold_days, float)
        w = p > 0
        return cls(float(w.mean()) if len(p) else 0.5, float(h[w].mean()) if w.any() else 0.0,
                   float(h[~w].mean()) if (~w).any() else 0.0)


def perp_cost_gate(*, e_gross: float, certain_cost: float, funding: FundingEstimate, hold: OutcomeWeightedHold,
                   k_cost: float, lam: float) -> dict:
    """E_gross >= k_cost x c + E[f x h_ow] + lambda x sd(f x h_ow). All as fractions of notional."""
    if not isinstance(hold, OutcomeWeightedHold):
        raise TypeError("hold must be outcome-weighted (winners and losers separately), not a single mean (INV-18)")
    if funding.kind != "CONDITIONAL":
        raise UnconditionalFunding("the cost gate reads conditional funding only (INV-17)")
    e_fh = funding.paid_per_day * (hold.p_win * hold.hold_win_days + (1 - hold.p_win) * hold.hold_loss_days)
    h2 = hold.p_win * hold.hold_win_days + (1 - hold.p_win) * hold.hold_loss_days
    sd_fh = funding.sd_per_day * math.sqrt(max(h2, 0.0))
    need = k_cost * certain_cost + e_fh + lam * sd_fh
    return {"pass": e_gross >= need, "certain_term": k_cost * certain_cost, "carry_term": e_fh,
            "carry_risk_term": lam * sd_fh, "need": need, "e_gross": e_gross, "assumed_funding": funding.assumed}


def funding_time_stop(cum_paid_R: float, flag_R: float, reduce_R: float) -> str:
    if cum_paid_R >= reduce_R:
        return "REDUCE"
    if cum_paid_R >= flag_R:
        return "FLAG"
    return "OK"
