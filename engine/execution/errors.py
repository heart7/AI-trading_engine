"""Venue error classification (spec §8.7 "Error mapping", INV-40).

Every venue error maps to exactly one class. Anything not recognised is UNKNOWN_STATE, never
REJECTED: an order whose fate is not known may be live on the venue, so resubmitting it could
double the position. UNKNOWN_STATE sends the OMS to reconciliation.
"""
from __future__ import annotations

from enum import StrEnum


class ErrorClass(StrEnum):
    RETRYABLE = "RETRYABLE"  # nothing happened on the venue; safe to retry with the same client id
    REJECTED = "REJECTED"  # the venue definitely refused the order
    UNKNOWN_STATE = "UNKNOWN_STATE"  # the order may or may not exist


class VenueError(Exception):
    def __init__(self, cls: ErrorClass, code: str, detail: str = ""):
        self.cls, self.code, self.detail = cls, code, detail
        super().__init__(f"{cls.value} {code} {detail}".strip())


class Timeout(VenueError):
    """A request whose response never arrived. Its effect is unknown by definition."""

    def __init__(self, detail: str = "request timed out"):
        super().__init__(ErrorClass.UNKNOWN_STATE, "TIMEOUT", detail)


def classify(table: dict[str, ErrorClass], code: str) -> ErrorClass:
    """Exact match first, then prefix (Kraken codes are 'ECategory:Message'). Unmapped -> UNKNOWN_STATE."""
    if code in table:
        return table[code]
    for k, v in table.items():
        if k.endswith("*") and code.startswith(k[:-1]):
            return v
    return ErrorClass.UNKNOWN_STATE
