"""Attribution, two orthogonal views, both exact (spec §12.2, INV-26).

Return decomposition per episode (Strategy A, spot long; benchmark is cash at 0, ASSUMED no interest):
  P0 benchmark                                         = 0
  BETA      = entry notional x beta x factor return over the hold
  SELECTION = P&L at decision prices (bar closes) - BETA
  EXECUTION = realised fill prices vs modelled fill prices (the §9.7 model), both legs
  CARRY     = funding (structural zero for spot A)
  COST      = -(modelled slippage + fees + tax reserve)
  ALPHA     = SELECTION + EXECUTION + CARRY + COST

Every term is a Decimal difference of exact inputs, so BETA + ALPHA equals net P&L with no residual term. The
engine also checks the sum against the ledger; any mismatch opens ATTRIBUTION_MISMATCH.
Process view: each episode is outcome variance or a process error (class regime / execution / data).
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal, localcontext

from engine.common.incidents import IncidentLog
from engine.evidence.ledger import D0, EXACT, dec

COMPONENTS = ("BETA", "SELECTION", "EXECUTION", "CARRY", "COST")
PROCESS_ERRORS = {"WRONG_SIZE": "execution", "STALE_INPUT": "data", "STOP_NOT_VENUE_RESIDENT": "execution",
                  "COST_MODEL_OFF": "execution", "REGIME_MISAPPLIED": "regime"}


@dataclass(frozen=True)
class Episode:
    episode_id: str
    sleeve: str
    qty: Decimal
    entry_decision: Decimal  # bar close at the decision
    entry_model: Decimal  # modelled fill price
    entry_fill: Decimal  # realised average fill price
    exit_decision: Decimal
    exit_model: Decimal
    exit_fill: Decimal
    fees: Decimal
    funding: Decimal = D0  # received positive (spot A: zero)
    tax_reserve: Decimal = D0
    beta: Decimal = Decimal(1)
    factor_return: Decimal = D0  # market factor return over the hold
    process_flags: tuple[str, ...] = ()

    @classmethod
    def of(cls, **kw) -> Episode:
        return cls(**{k: (dec(v) if k not in ("episode_id", "sleeve", "process_flags") else v) for k, v in kw.items()})

    @property
    def net(self) -> Decimal:
        with localcontext(EXACT):
            return self.qty * (self.exit_fill - self.entry_fill) - self.fees + self.funding - self.tax_reserve


def decompose(e: Episode) -> dict[str, Decimal]:
    with localcontext(EXACT):
        return _decompose(e)


def _decompose(e: Episode) -> dict[str, Decimal]:
    decision_pnl = e.qty * (e.exit_decision - e.entry_decision)
    beta = e.qty * e.entry_decision * e.beta * e.factor_return
    slip_model = e.qty * ((e.entry_model - e.entry_decision) + (e.exit_decision - e.exit_model))
    out = {
        "BETA": beta,
        "SELECTION": decision_pnl - beta,
        "EXECUTION": e.qty * ((e.exit_fill - e.exit_model) - (e.entry_fill - e.entry_model)),
        "CARRY": e.funding,
        "COST": -(slip_model + e.fees + e.tax_reserve),
    }
    out["ALPHA"] = out["SELECTION"] + out["EXECUTION"] + out["CARRY"] + out["COST"]
    return out


def process_label(e: Episode) -> tuple[str, str | None]:
    for f in e.process_flags:
        if f in PROCESS_ERRORS:
            return "PROCESS_ERROR", PROCESS_ERRORS[f]
    return "OUTCOME_VARIANCE", None


@dataclass(frozen=True)
class Attribution:
    by_component: dict[str, Decimal]
    net: Decimal
    by_process: dict[str, Decimal]
    exact: bool


def attribute(episodes: Iterable[Episode], *, ledger_net: Decimal | None = None,
              incidents: IncidentLog | None = None) -> Attribution:
    with localcontext(EXACT):
        return _attribute(episodes, ledger_net, incidents)


def _attribute(episodes: Iterable[Episode], ledger_net: Decimal | None, incidents: IncidentLog | None) -> Attribution:
    comp = {k: D0 for k in COMPONENTS + ("ALPHA",)}
    proc: dict[str, Decimal] = {}
    net = D0
    for e in episodes:
        d = decompose(e)
        for k, v in d.items():
            comp[k] += v
        label, cls = process_label(e)
        key = label if cls is None else f"{label}:{cls}"
        proc[key] = proc.get(key, D0) + e.net
        net += e.net
    exact = comp["BETA"] + comp["ALPHA"] == net and sum(proc.values(), D0) == net
    if ledger_net is not None:
        exact = exact and ledger_net.quantize(Decimal("0.01")) == net.quantize(Decimal("0.01"))
    if not exact and incidents is not None:
        incidents.open_incident("ATTRIBUTION_MISMATCH", "S2", "engine",
                                f"components {comp['BETA'] + comp['ALPHA']} vs net {net} vs ledger {ledger_net}")
    return Attribution(comp, net, proc, exact)
