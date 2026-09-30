# VERIFIER_HEARTBEAT_LOST

Class and severity: S1

**What fired.** No verifier heartbeat for 30 s.

**What the system already did.** Entries blocked until heartbeats return (dead-man).

**What you own.** Restart the verifier; check host health.

**Resolve when.** Heartbeats restored; block lifts automatically. Incidents are never dismissed; resolving one needs a written rationale (§7.9).
