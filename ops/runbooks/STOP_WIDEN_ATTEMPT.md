# STOP_WIDEN_ATTEMPT

Class and severity: S1

**What fired.** Something tried to move a stop away from price.

**What the system already did.** Refused (INV-04). The stop is unchanged.

**What you own.** Find the caller in the audit log. A code path that tries this is a bug: stop and fix before resuming.

**Resolve when.** Root cause found and fixed; rationale recorded. Incidents are never dismissed; resolving one needs a written rationale (§7.9).
