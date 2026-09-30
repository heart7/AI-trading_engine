"""Admissibility service C1 (spec §5.1, §6.2).

A pair is admissible at a bar close iff, in this order (the first failure is the binding reason):

  NOT_IN_UNIVERSE     not in the policy universe (only a policy version can add a pair)
  NOT_LISTED          no listing record live at the bar (point in time, INV-21)
  DELISTED            listing ended
  DELISTING_NOTICE    venue has announced a delisting
  BLACKOUT            inside a governance-owned blackout window (maintenance, forks, token unlocks; A-BLACKOUT)
  VENUE_STALE         heartbeat, book or trades older than their TTL
  TTF_EXCEEDED        portfolio time-to-flatten at the intended size above the ceiling (stressed depth, §7.5)
  COST_GATE           the §5.4 cost gate fails
  INSUFFICIENT_HISTORY  no signal yet (T undefined)

Checks whose input is not supplied are skipped and listed in the claim's `inputs` as "unchecked:<name>", so a
claim never says more than was checked. The output is an `admissibility_claim` (DERIVED).
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from engine.admissibility.ttf import admit_by_ttf
from engine.common.canonical import content_hash
from engine.common.schemas import validate
from engine.data.listings import Listing

ROOT = Path(__file__).resolve().parents[2]
BLACKOUT_FILE = ROOT / "policy" / "blackout.yaml"
REASONS = ("NOT_IN_UNIVERSE", "NOT_LISTED", "DELISTED", "DELISTING_NOTICE", "BLACKOUT", "VENUE_STALE", "TTF_EXCEEDED",
           "COST_GATE", "INSUFFICIENT_HISTORY")


@dataclass(frozen=True)
class Window:
    start: datetime
    end: datetime  # exclusive
    pairs: tuple[str, ...]  # bases, or ("*",) for all
    reason: str
    source: str

    def covers(self, base: str, t: datetime) -> bool:
        return self.start <= t < self.end and ("*" in self.pairs or base in self.pairs)


def _dt(v: Any) -> datetime:
    d = v if isinstance(v, datetime) else datetime.fromisoformat(str(v))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def load_blackout(path: Path = BLACKOUT_FILE) -> list[Window]:
    if not path.exists():
        return []
    doc = yaml.safe_load(path.read_text()) or {}
    out = []
    for w in doc.get("windows") or []:
        win = Window(_dt(w["start"]), _dt(w["end"]), tuple(str(p).upper() for p in w["pairs"]), str(w["reason"]), str(w["source"]))
        if win.end <= win.start:
            raise ValueError(f"blackout window ends before it starts: {w}")
        out.append(win)
    return out


@dataclass
class AdmissibilityService:
    policy: Mapping[str, Any]
    policy_hash: str
    listings: Mapping[str, Listing] | None = None  # base -> listing on the execution venue; None = unchecked
    blackout: Iterable[Window] = field(default_factory=list)

    def universe(self) -> set[str]:
        u = self.policy["universe"]["A"]
        return {str(b).upper() for b in list(u["mandatory"]) + list(u["default"])}

    def evaluate(self, base: str, bar_close: datetime, *, T: float | None = None, venue_fresh: bool | None = None,
                 positions_usd: Mapping[str, float] | None = None, add_usd: float = 0.0,
                 volume_usd_per_min: Mapping[str, float] | None = None, cost_gate: str | None | bool = False,
                 now: datetime | None = None, fixture: bool = False) -> dict[str, Any]:
        """`cost_gate`: False = unchecked, None = passed, a gate code = failed (as returned by the cost gate)."""
        base = base.upper()
        checked, fails = [], []

        def check(name: str, ok: bool | None) -> None:
            if ok is None:
                checked.append(f"unchecked:{name}")
                return
            checked.append(name)
            if not ok:
                fails.append(name)

        check("NOT_IN_UNIVERSE", base in self.universe())
        lst = None if self.listings is None else self.listings.get(base)
        if self.listings is None:
            for n in ("NOT_LISTED", "DELISTED", "DELISTING_NOTICE"):
                check(n, None)
        else:
            check("NOT_LISTED", lst is not None and lst.listed_at <= bar_close)
            check("DELISTED", lst is None or lst.delisted_at is None or bar_close < lst.delisted_at)
            check("DELISTING_NOTICE", lst is None or not lst.under_notice_at(bar_close))
        wins = [w for w in self.blackout if w.covers(base, bar_close)]
        check("BLACKOUT", not wins)
        check("VENUE_STALE", venue_fresh)
        if positions_usd is None or volume_usd_per_min is None or add_usd <= 0:
            check("TTF_EXCEEDED", None)
            ttf = None
        else:
            adm = admit_by_ttf(positions_usd, volume_usd_per_min, base, add_usd, self.policy["risk"]["ttf_ceiling_min"]["A"])
            ttf = adm.ttf_min
            check("TTF_EXCEEDED", adm.admissible)
        check("COST_GATE", None if cost_gate is False else cost_gate is None)
        check("INSUFFICIENT_HISTORY", T is not None)
        binding = fails[0] if fails else None
        detail = {"BLACKOUT": wins[0].reason if wins else None,
                  "DELISTING_NOTICE": lst.delisting_notice_at.isoformat() if lst and lst.delisting_notice_at else None,
                  "TTF_EXCEEDED": ttf}
        payload = {"pair": f"{base}/USD", "bar_close": bar_close.isoformat(), "admissible": binding is None,
                   "binding_reason": binding}
        now = now or datetime.now(timezone.utc)
        claim = {"claim_id": "ac-" + content_hash([payload, checked])[:16], "kind": "admissibility_claim",
                 "class": "DERIVED", "snapshot_hash": content_hash({"payload": payload, "checked": checked,
                                                                    "detail": {k: v for k, v in detail.items() if v is not None}}),
                 "policy_hash": self.policy_hash, "code_version": "0.1.0", "created_at": now.isoformat(),
                 "ttl_s": 4 * 3600, "certified": not fixture, "inputs": checked, "payload": payload}
        validate("admissibility_claim", claim)
        return claim

    def gate(self, base: str, bar_close: datetime) -> tuple[bool, str | None]:
        """The decision cycle's hook: the checks that need no live book (universe, listings, blackout)."""
        p = self.evaluate(base, bar_close, T=0.0, now=bar_close)["payload"]
        return p["admissible"], p["binding_reason"]
