# COST_INPUT_STALE

Class and severity: D2

**What fired.** A cost input (fee tier, spread, slippage quantile) was older than its TTL.

**What the system already did.** Entries blocked for the affected venue until inputs are fresh.

**What you own.** Check the venue's public data feed and the capability snapshot job.

**Resolve when.** Inputs fresh again; write what was stale and why. Incidents are never dismissed; resolving one needs a written rationale (§7.9).
