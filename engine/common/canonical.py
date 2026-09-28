"""Canonical serialisation and content hashing.

Every decision-plane artefact (policy, claim, snapshot) is identified by the SHA-256 of its
canonical JSON form, so the same content always has the same hash (spec §13.5).
"""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any


def _normalise(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {str(k): _normalise(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_normalise(v) for v in obj]
    if isinstance(obj, float):
        if not math.isfinite(obj):
            raise ValueError("non-finite float cannot be canonicalised")
        if obj.is_integer() and abs(obj) < 2**53:
            return int(obj)
        return obj
    if obj is None or isinstance(obj, (str, int, bool)):
        return obj
    raise TypeError(f"type {type(obj).__name__} is not canonicalisable")


def canonical_json(obj: Any) -> str:
    return json.dumps(_normalise(obj), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_hash(obj: Any) -> str:
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()
