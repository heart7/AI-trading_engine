# 0003 — The harness measures the signal with the Kelly cap off

Status: taken by the build (reversible), for owner review.

Spec §5.5 sets `kelly_cap = k × max(0, μ_q) / σ̂² × NAV`, where μ_q is the 25th-percentile posterior mean return
(prior at 0, n0 = 252), and says `μ_q ≤ 0 ⇒ size 0`. On 9 years of FIXTURE data, the walk-forward μ_q conditional on
the entry state is negative for every pair and every fold (−0.07% to −0.25% per day). The reason: only about
60–90 independent entry-state days exist per pair, and the n0 = 252 prior plus the 25th percentile overwhelm them.
With the cap on, the harness sees zero trades and can never test for edge. That is the same kind of deadlock as the
v9.1 step-6 loop.

Decision for the build: harness steps 2–7 replay the strategy with the Kelly cap disabled, which is a research-plane
flag (`kelly_enabled`). Production sizing keeps the cap. Its μ_q must then come from the validated posterior of the
sleeve's own daily returns (step 2 output), not from per-pair conditional returns. Until a step-2 PASS exists,
μ_q is not on file and live sizing is 0, which matches the spec's intent that the engine does not trade an unproven
signal.
