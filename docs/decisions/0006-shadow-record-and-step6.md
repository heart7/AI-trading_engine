# 0006 — How the shadow record is measured, and step 6 without Part 6A

Status: decided by default in P6 (reversible, no owner action needed). Recorded 2026-09-29.

1. **Cost divergence before any fill (A-SHADOW-COST-OBS).** §9.7 compares realised fills with the model's prediction
   for the same intents. SHADOW places no orders, so each would-be entry is priced twice: by the cost model
   (half-spread plus q75 slippage) and by Kraken's public order book read at the decision (half-spread plus 50 bp
   times the share of the ask depth within 50 bp the order would take). Divergence is
   |Σ observed − Σ predicted| / Σ predicted over the step. CANARY replaces the book reading with real fills.
2. **Which shadow events are H1/D1 (A-SHADOW-INCIDENTS).** §7.9 defines H (human/governance) and D (data) classes.
   A bar missing or uncertified at the cycle is D1; an engine/verifier disagreement on T is D1 `RECON_BREAK`; a policy
   change during SHADOW without a new signed policy is H1 `PARAMS_NOT_FROZEN`, and that cycle abstains. Recon is the
   share of instrument-cycles where the two agree to 1e-9. A day counts toward the 90 only when all six 4h cycles of
   that UTC day are in the journal at the SHADOW rung; PAPER-rung cycles are a pre-shadow record and never count.
3. **FIXTURE never counts.** A journal with any FIXTURE cycle is FIXTURE class. It feeds no gate: G2 reads zero days
   and the mode ladder refuses to promote on it.
4. **Step 6 (A-STEP6-N1-N4).** §9.3 defers N1–N4 to Part 6A, which was not supplied (§21). The build reads the
   one-line definitions as: N1 walk-forward calibration (ECE ≤ `regime.ece_warn`); N2 label-shuffle (real Brier skill
   above the 95th percentile of permuted-label skill, and no skill on permuted labels); N3 a context gets pool weight
   only with an out-of-sample Brier uplift whose block-bootstrap 5th percentile is above 0; N4 the regime-throttled
   shadow book's Sharpe minus the unthrottled book's, block-bootstrap 5th percentile above 0, on an OBSERVED shadow
   record of at least 90 days. Until that record exists step 6 is NOT RUN and the regime layer stays at T0. If Part 6A
   is supplied, these definitions are replaced by it.
5. **Mode ladder.** PROMOTE approvals sign the move, the policy hash and the step record together, so an approval
   cannot be reused for a different record. A failing gate other than dwell demotes one rung at review.
