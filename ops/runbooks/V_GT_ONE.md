# V_GT_ONE

Class and severity: S1

**What fired.** The volatility multiplier v came out above 1.

**What the system already did.** Sizing clamps v to 1 and the entry is sized as if v = 1 (INV-05).

**What you own.** Treat as a bug in the sizing inputs; check sigma* and the EWMA estimate.

**Resolve when.** Cause fixed; replay spot-check green. Incidents are never dismissed; resolving one needs a written rationale (§7.9).
