# VENUE_PERMISSION_VIOLATION

Class and severity: S1

**What fired.** A key or venue did something its recorded permissions forbid, or an order went to a venue that is not TRADE_ENABLED.

**What the system already did.** Refused; venue blocked.

**What you own.** Revoke the key at the venue if it has more permissions than recorded. Never add withdrawal scope.

**Resolve when.** Key re-probed and permissions match the record. Incidents are never dismissed; resolving one needs a written rationale (§7.9).
