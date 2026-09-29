# Runbooks (spec §7.9)

One page per incident class: what fired, what the system already did, what the human owns, and when it can be resolved.

Dead-man defaults act toward safety when nobody responds: an S1 unacknowledged for 2 h suspends the engine; the 16% drawdown rung undecided for 48 h flattens; a verifier heartbeat lost for 30 s blocks entries.

## Loss review cadence

| Review | Owner | Evidence |
|---|---|---|
| Pre-trade | engine | gate ladder persisted |
| Intraday | risk authority | limit utilisation snapshot |
| End of day | principal | net P&L, reconciliation status, abstentions |
| Weekly | principal | attribution, process errors, cost divergence |
| Monthly | principal | calibration, drift, hypothesis budget, stress battery, tier eligibility |
| Quarterly | principal (+ external reviewer when available) | regulatory facts, fee schedules, ASSUMED review dates |

## Incident classes

- [ADD_ON_LOSER](ADD_ON_LOSER.md) (S1)
- [ADL_EVENT](ADL_EVENT.md) (S2)
- [ATTRIBUTION_MISMATCH](ATTRIBUTION_MISMATCH.md) (D2)
- [BAR_MISSING](BAR_MISSING.md) (D1)
- [CAPABILITY_CHANGED](CAPABILITY_CHANGED.md) (H2)
- [CLOCK_SKEW](CLOCK_SKEW.md) (D2)
- [COLLATERAL_CAP](COLLATERAL_CAP.md) (S2)
- [CONTRACT_SPEC_CHANGED](CONTRACT_SPEC_CHANGED.md) (H2)
- [COST_INPUT_STALE](COST_INPUT_STALE.md) (D2)
- [COST_MODEL_DIVERGENCE](COST_MODEL_DIVERGENCE.md) (D2)
- [COST_R_EXCEEDED](COST_R_EXCEEDED.md) (S3)
- [DD16_UNDECIDED](DD16_UNDECIDED.md) (S1)
- [GEO_EGRESS_MISMATCH](GEO_EGRESS_MISMATCH.md) (S1)
- [LEDGER_CHAIN_BREAK](LEDGER_CHAIN_BREAK.md) (S1)
- [MARGIN_INVARIANT](MARGIN_INVARIANT.md) (S1)
- [NAV_DIVERGENCE](NAV_DIVERGENCE.md) (D1)
- [NO_BREAKOUT](NO_BREAKOUT.md) (info)
- [NO_ON_CALL](NO_ON_CALL.md) (H2)
- [ORDER_STATE_MISMATCH](ORDER_STATE_MISMATCH.md) (S2)
- [ORDER_STATE_UNKNOWN](ORDER_STATE_UNKNOWN.md) (S2)
- [PARAMS_NOT_FROZEN](PARAMS_NOT_FROZEN.md) (H1)
- [PROTECTION_UNVERIFIED](PROTECTION_UNVERIFIED.md) (S1)
- [RECON_BREAK](RECON_BREAK.md) (D1)
- [RUNG_CAPITAL_CAP](RUNG_CAPITAL_CAP.md) (info)
- [S1_UNACKED](S1_UNACKED.md) (S1)
- [STABLECOIN_DEPEG](STABLECOIN_DEPEG.md) (S2)
- [STOP_WIDEN_ATTEMPT](STOP_WIDEN_ATTEMPT.md) (S1)
- [TIER_DOWNGRADE](TIER_DOWNGRADE.md) (H2)
- [VENUE_PERMISSION_VIOLATION](VENUE_PERMISSION_VIOLATION.md) (S1)
- [VERIFIER_HEARTBEAT_LOST](VERIFIER_HEARTBEAT_LOST.md) (S1)
- [V_GT_ONE](V_GT_ONE.md) (S1)
- [WITHDRAWAL_KEY_REFUSED](WITHDRAWAL_KEY_REFUSED.md) (S1)
- [WS_RESYNC_PENDING](WS_RESYNC_PENDING.md) (D2)
