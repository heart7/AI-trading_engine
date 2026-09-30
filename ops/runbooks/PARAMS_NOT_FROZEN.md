# PARAMS_NOT_FROZEN

Class and severity: H1

**What fired.** The policy changed during SHADOW without a new signed policy.

**What the system already did.** The cycle abstains.

**What you own.** Put the frozen policy back, or sign the new one and restart the shadow record.

**Resolve when.** Policy hash matches the frozen one. Incidents are never dismissed; resolving one needs a written rationale (§7.9).
