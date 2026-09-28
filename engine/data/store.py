"""Append-only, hash-chained bar store (spec §11.2 WORM store, §13.2).

Local development stand-in for PostgreSQL/TimescaleDB + object storage with Object Lock: each
record is written once to a JSONL segment and chained by hash, so any edit breaks verification.
"""
from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

from engine.common.canonical import canonical_json, content_hash

GENESIS = "0" * 64


class ChainBroken(Exception):
    pass


class AppendOnlyLog:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._head = GENESIS
        if self.path.exists():
            for rec in self._read():
                self._head = rec["hash"]

    def _read(self) -> Iterator[dict]:
        with self.path.open() as f:
            for line in f:
                if line.strip():
                    yield json.loads(line)

    @property
    def head(self) -> str:
        return self._head

    def append(self, payload: dict) -> str:
        h = content_hash({"prev": self._head, "payload": payload})
        with self.path.open("a") as f:
            f.write(canonical_json({"prev": self._head, "payload": payload, "hash": h}) + "\n")
        self._head = h
        return h

    def records(self) -> list[dict]:
        return [r["payload"] for r in self._read()]

    def verify(self) -> int:
        prev, n = GENESIS, 0
        for rec in self._read():
            if rec["prev"] != prev or content_hash({"prev": prev, "payload": rec["payload"]}) != rec["hash"]:
                raise ChainBroken(f"record {n} breaks the hash chain")
            prev, n = rec["hash"], n + 1
        return n
