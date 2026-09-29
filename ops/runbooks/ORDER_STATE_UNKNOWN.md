# ORDER_STATE_UNKNOWN

Class and severity: S2

**What fired.** An order's state could not be determined after a timeout.

**What the system already did.** Treated as possibly live; no duplicate is sent; reconciliation queries it.

**What you own.** Check the venue's order list.

**Resolve when.** State known and reconciled. Incidents are never dismissed; resolving one needs a written rationale (§7.9).
