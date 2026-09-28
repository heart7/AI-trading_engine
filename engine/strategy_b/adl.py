"""Auto-deleveraging (spec §6.7, INV-22). An ADL close is an incident with its own classification and is excluded
from any training set; a job that receives one fails."""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from engine.common.incidents import IncidentLog


class ADLInTrainingSet(Exception):
    pass


@dataclass(frozen=True)
class AdlState:
    new_exposure_mult: float
    reduce_only: bool


def adl_controls(policy: Mapping[str, Any], queue: Mapping[str, str]) -> AdlState:
    """queue: position -> LOW | RISING | HIGH (OBSERVED where the venue publishes it)."""
    adl = policy["perps"]["adl"]
    high = sum(1 for q in queue.values() if q == "HIGH")
    rising = any(q in ("RISING", "HIGH") for q in queue.values())
    return AdlState(adl["reduce_new_exposure"] if rising else 1.0, high >= adl["reduce_only_min_positions"])


def record_adl_close(incidents: IncidentLog, episode: dict[str, Any], venue: str) -> dict[str, Any]:
    incidents.open_incident("ADL_EVENT", "S2", f"venue:{venue}", f"position {episode.get('episode_id')} closed by ADL")
    return dict(episode, classification="ADL", exit_reason="ADL", excluded_from_training=True)


def training_set(episodes: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    out: list[Mapping[str, Any]] = []
    for e in episodes:
        if e.get("classification") == "ADL" or e.get("exit_reason") == "ADL":
            raise ADLInTrainingSet(f"episode {e.get('episode_id')} is an ADL close")
        if not e.get("excluded_from_training"):
            out.append(e)
    return out


def exclude(episodes: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """What a learner job calls before training."""
    return [e for e in episodes if e.get("classification") != "ADL" and not e.get("excluded_from_training")]
