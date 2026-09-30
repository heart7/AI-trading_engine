"""Hypothesis registry (spec §10.2 L4) with the annual trial budget (§9.4)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from engine.common.schemas import validate

REGISTRY_FILE = Path(__file__).with_name("hypotheses.yaml")


class BudgetExceeded(Exception):
    pass


class LearnerHalted(Exception):
    """Calibration or drift is FAIL: the learner's budget halts until the monitor clears (§10.3a)."""


class GuardrailMissing(Exception):
    pass


# A change to any of these needs the named guardrail metric (INV-18): tightening the funding time stop cuts
# funding but also cuts the long-held winners the strategy lives on.
REQUIRED_GUARDRAILS = {"perps.funding_time_stop_R": "tail_contribution_share"}


def check_guardrails(h: dict[str, Any]) -> None:
    for prefix, metric in REQUIRED_GUARDRAILS.items():
        if any(c.startswith(prefix) for c in h.get("changes", [])) and metric not in h.get("guardrails", []):
            raise GuardrailMissing(f"{h['id']}: a change to {prefix} must carry the {metric} guardrail")


def load(path: Path = REGISTRY_FILE) -> list[dict[str, Any]]:
    items = yaml.safe_load(path.read_text())["hypotheses"]
    for h in items:
        validate("hypothesis", h)
    ids = [h["id"] for h in items]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate hypothesis id")
    return items


def budget_used(items: list[dict[str, Any]], year: int) -> int:
    return sum(h["budget_debit"] for h in items if str(h["registered_at"]).startswith(str(year)))


def register(items: list[dict[str, Any]], new: dict[str, Any], *, budget_per_year: int,
             halted: str | None = None) -> list[dict[str, Any]]:
    if halted:
        raise LearnerHalted(halted)
    validate("hypothesis", new)
    check_guardrails(new)
    year = int(str(new["registered_at"])[:4])
    if budget_used(items, year) + new["budget_debit"] > budget_per_year:
        raise BudgetExceeded(f"{year}: {budget_used(items, year)} + {new['budget_debit']} > {budget_per_year}")
    if any(h["id"] == new["id"] for h in items):
        raise ValueError(f"duplicate hypothesis id {new['id']}")
    return [*items, new]
