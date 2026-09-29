# COST_MODEL_DIVERGENCE

Class and severity: D2

**What fired.** Observed costs diverged from the cost model by 25% or more over the step.

**What the system already did.** Mode ladder demotes one rung at review; prior backtests are invalid until re-run with the recalibrated model.

**What you own.** Recalibrate the cost model from the observed fills or book readings, re-run the harness.

**Resolve when.** Model recalibrated and harness re-run. Incidents are never dismissed; resolving one needs a written rationale (§7.9).
