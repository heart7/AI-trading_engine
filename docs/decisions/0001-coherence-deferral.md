# 0001 — Evidence-dependent coherence checks bind from SHADOW

Status: proposed by the build, awaiting the principal's decision (spec conduct rule 0.2.6: ask, don't guess).

## Problem
Spec §19 makes "coherence check passes" the P0 exit gate. Three §20.2 checks need evidence that cannot exist in P0:

| Check | Needs | Earliest phase |
|---|---|---|
| C08 `sigma_star.mc_run_id` set and P(DD ≥ 20%) ≤ 5% | harness step 8 Monte Carlo | P2b |
| C13 every `execution_allowed` venue is TRADE_ENABLED with an access record | Kraken connector, access record | P3 / P7 |
| C15 stress battery PASS attached | stress battery (§7.8) | P3.5 |

Read literally, the shipped v10.4.0 policy can never be coherent in P0.

## Decision taken for the build
The three checks are always evaluated and always shown. They **block activation for SHADOW, CANARY and LIVE**.
For a PAPER activation they show as `DEFERRED` and do not block. PAPER sends no orders (INV-35 rejects any order
to a venue that is not TRADE_ENABLED), so deferring them cannot put capital at risk. All structural checks
(C01–C06, C09–C12, C14, C16) bind in every mode, PAPER included.

## Alternative
Keep all checks binding in every mode and move the P0 exit gate to "structural checks pass". Same effect, different wording.
