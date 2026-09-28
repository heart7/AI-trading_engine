"""Incident log (spec §14). Incidents are append-only; blocking scopes are derived from open incidents."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Incident:
    code: str
    severity: str  # S1 (page now) .. S4
    scope: str  # "venue:<id>", "engine", ...
    detail: str
    opened_at: datetime = field(default_factory=_now)
    resolved_at: datetime | None = None
    resolution: str = ""

    @property
    def open(self) -> bool:
        return self.resolved_at is None


@dataclass
class IncidentLog:
    items: list[Incident] = field(default_factory=list)

    def open_incident(self, code: str, severity: str, scope: str, detail: str) -> Incident:
        inc = Incident(code, severity, scope, detail)
        self.items.append(inc)
        return inc

    def resolve(self, code: str, scope: str, resolution: str) -> int:
        n = 0
        for inc in self.items:
            if inc.open and inc.code == code and inc.scope == scope:
                inc.resolved_at, inc.resolution = _now(), resolution
                n += 1
        return n

    def open_codes(self, scope: str | None = None) -> set[str]:
        return {i.code for i in self.items if i.open and (scope is None or i.scope == scope)}
