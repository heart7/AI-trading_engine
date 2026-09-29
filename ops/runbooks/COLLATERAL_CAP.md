# COLLATERAL_CAP

Class and severity: S2

**What fired.** A venue, issuer or reserve limit was breached (INV-16).

**What the system already did.** Entries blocked on the venue (or engine-wide for the reserve); a sweep transfer_intent is proposed.

**What you own.** Execute the proposed transfer yourself; the engine holds no withdrawal keys.

**Resolve when.** Back inside every cap. Incidents are never dismissed; resolving one needs a written rationale (§7.9).
