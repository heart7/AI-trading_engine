# 0007 — Trade episodes and the cost-model retune

Status: decided by default in P8 (reversible, no owner action needed). Recorded 2026-09-30.

The learning loop (spec §10) keeps the propose-then-sign design: nothing the learner produces changes a live
parameter. This decision covers the first two stages of the §10.3a pipeline that were not yet built.

1. **Episode factory (L1).** Every closed position becomes a `trade_episode` (schema `schemas/trade_episode.json`).
   Each side is priced three ways: the bar close the decision was made on, the model's predicted fill from the
   arrival price, and the realised fill. Spread plus slippage is measured against the arrival price; fees are kept
   apart. Episodes go into an append-only, hash-chained store (`runs/learning/episodes.jsonl`), and adding the same
   episode twice is a no-op. The L2 attribution runs on the same records and must sum exactly.
2. **Exclusions (§10.3).** ADL_EVENT, FORCED_FLATTEN, TIER_DOWNGRADE_EXIT and PROTECTION_FAILURE episodes are kept for
   incident analysis and never enter a training set. FIXTURE episodes (from the paper replay, where fills equal the
   model by construction) never train anything.
3. **COST_MODEL_OFF (A-EPISODE-COST-OFF).** A side that paid more than twice the predicted cost (1 bp floor) is a
   process error of class execution, so the process-error rate the spec measures learning by (§10.4) moves with it.
4. **Cost retune (A-COST-RETUNE, hypothesis H-A-COST-QUANTILE).** The fitted value is the policy quantile (q75) of the
   realised adverse cost per taker side, with a seeded 90% bootstrap interval, compared with the model's charge
   `half_spread + slippage_q75`. It needs 50 real fills or 100 OBSERVED shadow quotes.
   - Interval wholly above the charge: propose RAISE (stricter model).
   - Interval wholly below it: propose LOWER only with at least 50 real fills. Shadow quotes carry an ASSUMED impact
     model (A-SHADOW-COST-OBS), so they may tighten the model but never loosen it. A LOWER is flagged as loosening
     and requires re-running §9.3 step 4 on the new value.
   - Otherwise NO_CHANGE.
   The proposal is HYPOTHESIS class with `applies: false`, and `--history` replays the stored bars under it to show
   what it would have changed. cost_R_max and N_max are never targets (§10.3c).
5. **Applying a proposal** is a code or policy change like any other: harness, stress PASS for the new hash, signed
   POLICY_ACTIVATE. Until CANARY there are no real fills, so the only OBSERVED input is the shadow order-book record.
