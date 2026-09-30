# CLOCK_SKEW

Class and severity: D2

**What fired.** Local clock differs from the venue by more than 500 ms.

**What the system already did.** Entries blocked on that venue.

**What you own.** Check NTP on the host.

**Resolve when.** Skew back under 500 ms. Incidents are never dismissed; resolving one needs a written rationale (§7.9).
