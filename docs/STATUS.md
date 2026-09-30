# Build status (four-way, conduct rule 0.2.5)

Legend: **RV** implemented + run-verified · **IU** implemented, unverified · **SN** specified, not implemented · **UQ** unresolved design question.
"Run-verified" means a run record exists: CI uploads `runs/ci/tests.json` (command, environment hash, timestamps, output hash) and `runs/ci/invariants.json` on every push.

## P0 Foundations

| Deliverable | Status | Where |
|---|---|---|
| JSON-Schema single source: policy object, signal/admissibility/regime/sizing/tier claims, signal_intent, agent_activity_event, hypothesis, approval, run record | RV | `schemas/` |
| Policy object v10.4.0 (verbatim §20.1), YAML 1.2 number parsing, content hash | RV | `policy/policy-10.4.0.yaml`, `engine/policy/loader.py` |
| Coherence checks §20.2 (C01–C15) plus C16 drawdown rungs monotone | RV | `engine/policy/coherence.py` |
| Evidence checks C08/C13/C15 in PAPER | decided by default (2026-09-29) | decision 0001: DEFERRED in PAPER, bind from SHADOW |
| Single-signer approvals: hardware-key (FIDO2 `sk-`) SSHSIG verification, user-presence flag, rationale inside the signed bytes, counter replay check, cooling-off field honoured (0h) | RV | `engine/governance/` |
| Principal's hardware keys enrolled (primary + backup) | SN — owner action | `docs/OWNER-ACTIONS.md` |
| Policy v10.4.0 signed by the principal | SN — owner action | `docs/OWNER-ACTIONS.md` |
| Hypothesis registry seeded with pre-registered steps 2–5 and MDE; annual budget enforced | RV | `research/registry/` |
| ASSUMED-parameter register with owner + review date | RV (content), SN (UI chip) | `policy/assumed_register.yaml` |
| Negative-test suite: all 43 invariants registered; uncovered ones xfail and are listed as NOT_IMPLEMENTED | RV | `tests/negative/` |
| CI: secret scan, lint, policy coherence, tests with run record | IU until the first Actions run | `.github/workflows/ci.yml` |
| Secret store interface; in-memory store; Vault KV v2 client | RV (interface, in-memory), IU (Vault) | `engine/secrets/store.py` |

## Stack substitutions (spec §13.2 allows them with a recorded rationale)
- Local development runs Python 3.11; CI runs 3.12 as specified.
- Hardware-key approvals use OpenSSH FIDO2 signatures for the CLI and out-of-band path; WebAuthn in the browser arrives with the UI (P5). Both check the same authenticator user-presence flag.

## Environment limits seen while building
- The build container cannot reach exchange APIs (Kraken, Binance and Bybit connections are refused by its network policy). P1 ingest code will be built and tested against FIXTURE data; certifying real history needs a runner with exchange access or a data vendor (spec §21 item 11).

## P1 Data

| Deliverable | Status | Where |
|---|---|---|
| 4h bar model, UTC alignment, daily bars aggregated from 4h (incomplete days dropped, never filled) | RV | `engine/data/bars.py` |
| Certification: two-source agreement, single-venue flag, invalid-bar quarantine, gap report, dispersion > 3× normal alert, venue splices, era tags, content hash; FIXTURE never certified | RV | `engine/data/certify.py` |
| Quality report per dataset; build without one cannot be certified | RV | `engine/data/certify.py` |
| Point-in-time listing table; constant universe rejected (INV-21) | RV | `engine/data/listings.py` |
| Append-only hash-chained bar store (local WORM stand-in) | RV | `engine/data/store.py` |
| Kraken / Binance / Bybit public 4h OHLCV parsers; USDT re-quoted to USD at a certified rate, never par | RV (parsers), IU (live fetch) | `engine/data/sources/public_ohlc.py` |
| FIXTURE dataset build (4 pairs × 9 years) | RV | `tools/build_dataset.py --fixture` |
| History back to earliest listing; perps funding/specs ingest | SN — blocked | needs exchange network access and a history vendor (§21 item 11) |
| **P1 exit gate** (quality reports shipped for real data; listing table complete) | not met | blocked on data access |

## P2 Decision core (Strategy A)

| Deliverable | Status | Where |
|---|---|---|
| Shared signal T=(B+M+Z)/3, closes-only breakout, 180-day history rule; scalar and vectorised paths agree to 1e-12; causality property test | RV | `engine/signal/trend.py` |
| Cost gate §5.4 (stale input → COST_INPUT_STALE, no bypass parameter) | RV | `engine/sizing/cost.py` |
| Sizing §5.5: risk, Kelly cap, ES97.5, liquidity, exposure map, spot gross ≤ 1×NAV, v = min(1, σ*/σ_book), regime only at T1 | RV | `engine/sizing/sizing.py` |
| Stops §5.6: initial, ratcheting trail, time stop, never widen, lot/tick rounding with R recompute | RV | `engine/stops/stops.py` |
| Gate ladder §7.1 (17 gates, first failure binding), exit rule with no evidence input, add rule §5.7, cluster cap | RV | `engine/risk/gates.py` |
| Loss ladder §7.3 (daily, 5-day, 20-day, DD 8/12/16/20 with 48h dead-man), kill switches, signed re-arm | RV (unit), IU (latching over long replays) | `engine/risk/ladder.py` |
| Strategy Router §3: gates G1–G9, active = min(eligible, selected), notify-only on floor crossing, signed upgrade, auto-downgrade, deposit dwell restart | RV | `engine/router/router.py` |
| Decision clock: no intraday path | RV | `engine/portfolio/clock.py` |
| Replay/PAPER engine with §9.7 fill model, persisted gate ladders, funnel counts | RV on FIXTURE | `engine/replay/paper.py` |
| Reproducibility spot-check (golden hash) | RV locally, in CI from this PR | `tools/repro_check.py` |
| Admissibility service §5.1 (time-to-flatten, blackout calendar, delisting notice) | SN — needs order-book data | P3 |
| Gap policy for real data inside the 180-day signal window | UQ | see note |

**Findings from the fixture replay (FIXTURE data, REPORTED class, not evidence):**
- The ES limit (ES97.5 1-day ≤ 1.5 × r_tier × NAV) binds at about one full position. Appendix B.1 expects 25–35% gross with 2–3 positions, which this limit does not allow. A spec decision is needed before P2b results mean anything.
- The per-venue cap of 40% NAV, minus the 10% operating buffer, leaves 30% NAV for Strategy A positions on Kraken.
- Kelly sizing needs μ_q. Without it the spec gives size 0. P2b will estimate μ_q walk-forward; the fixture replay uses an ASSUMED 0.1% per day.
- **Gap policy:** real data will have quarantined bars. Today a gap makes the instrument DATA_STALE, and the signal needs a gap-free window. The rule for signals that span a gap is an open question.

## P2b Validation A

| Deliverable | Status | Where |
|---|---|---|
| Stats: stationary block bootstrap, Sharpe LB, DSR, MinTRL (matches Appendix B.4), paired Sharpe CI, n_eff | RV | `research/harness/stats.py` |
| Walk-forward μ_q with embargo (reported; not used by the null test, see decision 0003) | RV | `research/harness/walkforward.py` |
| Steps 1–5, 7, 8 with run records (config hash, data hash, code version, seed) | RV on FIXTURE | `research/harness/steps.py`, `tools/run_harness.py` |
| σ* Monte Carlo with null-edge mass, t(4) shocks, loss ladder, ladder-off control (§9.9) | RV | `research/montecarlo/sigma_star.py` |
| Verdict engine: NOT RUN without run_id, no PASS on CI ≤ 0, offline-only evidence, step 6 never gates SHADOW, promotion caps | RV | `research/harness/verdicts.py` |
| Verdicts on real history | SN — blocked on data | P1 |
| σ* choice when the ladder dominates | UQ | `docs/validation/fixture-harness-2026-09-28.md` |

## P3 Execution (PAPER only; no order has been sent to any real venue)

| Deliverable | Status | Where |
|---|---|---|
| Connector registry: 6 shipped types + `ccxt-generic` (data/PAPER only, never trade-enabled) | RV | `engine/execution/registry.py` |
| Venue lifecycle DRAFT → CONNECTED_READ → PAPER_ENABLED → TRADE_ENABLED, SUSPENDED, REMOVED; signed TRADE_ENABLE, ACCESS_RECORD, CAPABILITY_APPROVE, VENUE_REMOVE | RV | `engine/execution/venues.py` |
| Key slots, least privilege, withdrawal refused, daily re-probe → SUSPENDED + S1, 90/180-day rotation | RV | `engine/execution/venues.py` |
| Permission-probe parsers (Binance apiRestrictions, Bybit query-api, Kraken attested + negative probe); fail closed on unexpected shapes | RV (parsers), IU (live responses) | `engine/execution/adapters/probes.py` |
| Access record §8.6 and geo rule INV-43 (residence = GB, venue confirmation required, egress country check) | RV | `engine/execution/venues.py` |
| Capability snapshot diff → entries blocked until re-approved | RV | `engine/execution/venues.py` |
| OMS: idempotent client ids, journal-before-send, retry only RETRYABLE, UNKNOWN → reconciliation (never resubmit), fencing epoch, restart reconciliation, stream-gap REST resync, dust write-off | RV on the simulated venue | `engine/execution/oms.py` |
| Entry ladder: post-only at touch, re-price, IOC limit at the 15-min deadline, capped by sizing; never market | RV | `engine/execution/ladder.py` |
| Venue-resident stops sized from filled qty; read-back verifier → PROTECTION_UNVERIFIED blocks entries; cancel/fill race safe (amend and cancel-replace paths) | RV on the simulated venue | `engine/execution/oms.py` |
| Rate-limit token bucket with priority lanes; venue usage overrides local estimate | RV | `engine/execution/ratelimit.py` |
| Clock skew > 500 ms → CLOCK_SKEW, entries blocked | RV | `engine/execution/oms.py` |
| Dead-man switch for working entries only; not used where the timer would cancel stops | RV (sim) | `engine/execution/ladder.py` |
| Simulated venue with scripted fault injection; 12-seed fuzz across faults and restarts | RV | `engine/execution/sim.py`, `tests/unit/test_execution.py` |
| Conformance suite (12 checks) with content-hashed run record; runs in CI | RV on sim | `engine/execution/conformance.py`, `tools/uchfe.py conformance` |
| Kraken spot adapter: signing (matches Kraken's published example), order params, error map, read-back | IU — never called a real endpoint | `engine/execution/adapters/kraken_spot.py` |
| Binance spot / Bybit v5 read-only account adapters for tax history (no order methods) | IU | `engine/execution/adapters/readonly.py` |
| Conformance on Kraken demo / Binance and Bybit testnets | SN — blocked | needs exchange network access and demo/testnet keys (owner) |
| Kraken `CancelAllOrdersAfter` sparing stop-loss orders | UQ | if it cancels stops, the dead-man is not used on Kraken (spec §8.7) |
| Perp connectors (binance-usdm, bybit-v5-linear, kraken-futures) order paths | SN | Strategy B is PAPER-only; registered, not implemented |
| **P3 exit gate** (conformance green per connector on testnet/demo; race tests; INV-30, 31, 35–40 green) | partly met | race tests and invariants green; testnet conformance blocked |

## P3.5 Evidence plane

| Deliverable | Status | Where |
|---|---|---|
| Double-entry, append-only, hash-chained ledger (Decimal, idempotent per source record, chain break → S1 incident) | RV | `engine/evidence/ledger.py` |
| NAV dual path (ledger vs venue balances) with 0.1% divergence → NAV_DIVERGENCE, entries blocked | RV | `engine/evidence/nav.py` |
| Unitised TWR, HWM and drawdown on unit value, IRR; deposits never P&L (INV-41) | RV | `engine/evidence/performance.py` |
| Attribution P0–P5 (BETA, SELECTION, EXECUTION, CARRY, COST) and process view, exact, checked against the ledger (INV-26) | RV; the fixture replay sums to the penny | `engine/evidence/attribution.py`, `engine/evidence/drills.py` |
| Collateral policy: venue cap, issuer cap, par band, off-exchange reserve, human-executed transfer intents (INV-16) | RV | `engine/evidence/collateral.py` |
| Stress battery S1–S10 with run record; proposals need a PASS for their own hash (INV-42) | RV; replay scenarios run as synthetic proxies until history is certified | `engine/evidence/stress.py`, `engine/governance/proposals.py` |
| S4 venue failure vs 40% venue cap | default chosen: unsigned v10.4.1 proposal, cap 20% (stress PASS) | decision 0004 |
| Verifier: independent signal, NAV and risk recompute → RECON_BREAK; watchdog 30 s heartbeat | RV; signal agrees with the engine to 1e-15 on fixtures | `engine/evidence/verifier.py` |
| Drill "kill verifier → entries block" | RV (CI) | `tools/uchfe.py drill` |
| Encrypted snapshot (AES-256-GCM, key in the secret store), restore with chain check, restore drill | RV | `engine/evidence/backup.py` |
| Hot standby with fencing token checked in the OMS | RV (fencing, P3) | `engine/execution/oms.py` |
| Postgres synchronous replica, WAL archive, Object Lock storage, on-call rota | SN | infrastructure, not in this repo yet |
| **P3.5 exit gate** (kill-verifier drill; attribution to the penny on replay) | met on FIXTURE | CI runs both drills |

## P4 Regime layer + Strategy B core (PAPER)

| Deliverable | Status | Where |
|---|---|---|
| CCMRM regime layer: U/D/R/S states (rule ASSUMED), 2-bar confirmation, decayed counts over H, Dirichlet posteriors with 90% intervals, ESS, throttle mapping (reproduces the v9.1 fixtures), binding reasons, schema-valid `regime_claim` | RV | `engine/regime/ccmrm.py` |
| Authority T0: sizing multiplier is always 1, no stress cut | RV | `RegimeLayer.sizing_multiplier` |
| Era homogeneity chi-square, information horizon k*, ECE calibration | RV (functions), SN (UI wiring) | `engine/regime/ccmrm.py` |
| Step 6 (N1–N4 regime validation) | RV on FIXTURE (P6) | `research/harness/step6.py` |
| Contract spec versioning: material change → entries blocked, liquidation distances recomputed, re-approval (INV-25) | RV | `engine/strategy_b/contracts.py` |
| Conditional funding per book, zero-or-adverse default, outcome-weighted hold, perp cost gate with carry and carry-risk terms (INV-17, 18) | RV | `engine/strategy_b/funding.py` |
| Funding interval only from the spec; lint test for hard-coded intervals (INV-24) | RV | `tests/negative/test_p4_invariants.py` |
| Sizing §6.6: isolated margin only (INV-15), liquidation buffer, leverage cap, Σ margin ≤ φ × hard DD (INV-14), gross and net caps | RV | `engine/strategy_b/margin.py` |
| ADL controls, ADL_EVENT incident, excluded from training (INV-22) | RV | `engine/strategy_b/adl.py` |
| Admission by portfolio time-to-flatten and B universe filters (INV-23) | RV | `engine/admissibility/ttf.py` |
| Deflation parameter by test type (INV-19); time-stop hypotheses need the tail-share guardrail (INV-18) | RV | `research/harness/stats.py`, `research/registry/registry.py` |
| B_short / B_long PAPER replay; harness steps 1–5, 7, 8 per book | RV on FIXTURE | `engine/replay/perp.py`, `tools/run_harness.py --sleeve` |
| B verdicts | on file (FIXTURE): both books PAPER | `docs/validation/fixture-harness-b-2026-09-28.md` |
| Perp connector order paths (binance-usdm, bybit-v5-linear, kraken-futures) | SN | registered; PAPER only, and execution_allowed is empty for B |
| Real funding history and perp specs | SN — blocked | P1 data access |
| **P4 exit gate** (INV-14..25 green; B verdicts on file) | met on FIXTURE | |

## P5 UI + Reporter

| Deliverable | Status | Where |
|---|---|---|
| Read-only BFF: every §15.3 and §16.8.2 projection as GET, `POST /v1/reporter/ask` the only other route, `?cursor=` masks any GET server-side | RV | `engine/bff/server.py`, `engine/bff/projections.py` |
| Paper session: fixture universe through the real decision code, evidence plane and regime layer at T0 | RV on FIXTURE | `engine/bff/session.py` |
| Class-bound rendering (§16.6): every figure is a claim rendered by the BFF; each rule has a negative test; FIXTURE hatched with `certified: false` (INV-32) | RV | `engine/ui/render.py`, `tests/negative/test_p5_invariants.py` |
| `agent_activity_event` generation: one pointer per claim, intent, order transition and read-back; schema-valid | RV | `engine/bff/activity.py` |
| Activity screen (§16.8) P1–P7, ported from the reference mock onto the projections; all §16.8.3 acceptance tests | RV (Python); browser check runs where Chromium exists | `engine/ui/static/activity.js`, `tests/unit/test_bff.py` |
| The other 14 screens and the Reporter side panel | RV on FIXTURE | `engine/ui/static/app.js` |
| Reporter: refuses EXECUTE requests, groundedness filter strips numbers not in the bundle and logs RECON_BREAK, REPORTED class | RV | `engine/bff/reporter.py` |
| No screen computes or formats a material figure (static scan: no `toFixed`, no inline handlers) | RV | `tests/negative/test_p5_invariants.py` |
| Lane count, SSE stream, cycle clock, T band | decided by default | decision 0005 |
| Real data on screen | SN — blocked | P1 data access |
| **P5 exit gate** (rendering negative tests green; no screen computes a material figure) | met on FIXTURE | |

Run it: `python -m engine.bff.server` then open http://127.0.0.1:8710/ (PAPER, FIXTURE, localhost only).

## P6 Shadow

| Deliverable | Status | Where |
|---|---|---|
| Mode ladder PAPER → SHADOW → CANARY → LIVE 25/50/100 %: one rung at a time, hardware-signed PROMOTE bound to the move and its step record, step gates (dwell, recon ≥ 99.9%, cost divergence < 25%, zero H1/D1, OBSERVED record), one-rung demotion, capital caps | RV | `engine/modes/ladder.py` |
| Step 6 never gates SHADOW (INV-29), now also at the ladder | RV | `tests/unit/test_shadow.py` |
| Shadow runner: frozen policy hash (H1 `PARAMS_NOT_FROZEN`), certified-bar check (D1), same decision code as PAPER, verifier recon per instrument (D1 `RECON_BREAK`), would-be entries priced by model and by the public order book | RV on FIXTURE | `engine/shadow/runner.py` |
| Append-only, hash-chained shadow journal | RV | `ShadowJournal` |
| Shadow metrics → step evidence and G2 inputs; FIXTURE never counts | RV | `engine/shadow/metrics.py` |
| Live keyless feed: store top-up from Kraken, Binance, Bybit public bars (≥ 2 sources), Kraken public order book | RV on recorded payload shapes; live fetch unverified (no exchange access from the build container) | `engine/shadow/feed.py` |
| Operator tool: `drill` (CI), `run` (one cycle, cron every 4h), `status`, `step6` | RV (drill) | `tools/shadow.py` |
| Step 6 N1–N4 (definitions ASSUMED, Part 6A not supplied) | RV on FIXTURE; N4 NOT RUN until an OBSERVED 90-day shadow record exists | `research/harness/step6.py`, decision 0006 |
| Shadow learner comparison: registered hypothesis only, forbidden parameters refused, live policy provably untouched, `applies: false` | RV | `research/learner/shadow_compare.py` |
| Decision defaults: 0001 applied; 0002 keep 1.5; 0004 option 1 as an unsigned proposal (v10.4.1, venue cap 20%, stress battery PASS for its own hash) | recorded | `docs/decisions/`, `policy/proposals/` |
| Validation screen showing the shadow record | RV | `shadow_record` in `engine/bff/projections.py`; empty until a journal exists |
| **P6 exit gate** (≥ 90 days SHADOW with recon ≥ 99.9%, cost divergence < 25%, zero H1/D1) | not met | needs §9.3 steps on real history, then 90 days of live public data |

Run the drill: `python tools/shadow.py drill`. On a machine with exchange access: see `docs/OWNER-ACTIONS.md` §5.

## P7 Canary → Live (offline parts)

| Deliverable | Status | Where |
|---|---|---|
| UK CGT matching per asset across all accounts: same day, 30 days (after every same-day match), section 104 pool; GBP at the certified daily rate; fees in allowable cost; tax years 6 Apr–5 Apr | RV on constructed cases | `engine/tax/uk.py` |
| Reserve above the annual exempt amount; UNSET (never guessed) while the rate or exempt amount is undeclared | RV | `policy/tax/tax-uk-v1.yaml` (ASSUMED values null) |
| Tax export: per-disposal matching CSV for the accountant, per-year summary | RV | `tools/tax_report.py` |
| Perps tax treatment, loss carry-forward, Nigeria double-taxation position | SN — ASSUMED, for the accountant | §12.4 |
| New-sleeve ramp after an upgrade: half risk budget for 30 days at CANARY/LIVE (§3.5) | RV | `StrategyRouter.ramp_factor` |
| Go-live checklist on the Governance screen | RV | `engine/bff/projections.py` |
| Canary and live-ramp capital caps per rung; nobody on call from CANARY up → STOP | RV | `engine/modes/canary.py`, `policy/oncall.yaml` (empty) |
| Profit allocation: reinvest % change needs a signed PROFIT_ALLOCATION approval over the exact change; sweeps are human-executed transfer intents after the tax reserve | RV | `engine/governance/profit_allocation.py` |
| Runbooks for every incident class the engine can open, plus dead-man defaults and the loss-review cadence; incident rows link their runbook | RV (test: no incident without a runbook) | `ops/runbooks/` |
| Go-live checklist (coherence for LIVE, signed go-live hash, §9.3 steps, shadow and canary records, accountant sign-off bound to the tax config hash, reserve declared, on-call cover, access record) | RV; today 0 of 9 met | `engine/governance/golive.py`, `tools/golive.py` |
| EOD and monthly reports from claims only, references appendix, REPORTED narrative citing figure ids, stress battery and ASSUMED items due | RV on FIXTURE | `engine/reports/reports.py`, `tools/report.py` |
| **P7 exit gate** (go-live policy hash signed; accountant sign-off on tax config) | not met | owner and accountant actions; P6 record first |


## P8 Learning loop (episodes and cost retune)

| Deliverable | Status | Where |
|---|---|---|
| Episode factory: each closed position as a `trade_episode`, both sides priced at decision, model and fill; exclusions per §10.3; COST_MODEL_OFF process errors | RV on FIXTURE | `research/learner/episodes.py`, `schemas/trade_episode.json` |
| Append-only, hash-chained, idempotent episode store; training set is OBSERVED and non-excluded only | RV | `EpisodeStore` |
| Attribution (L2) on the same episodes, exact to the replay net | RV | `episodes.to_attribution` |
| Cost-model retune from realised slippage: q75 with a seeded bootstrap interval; RAISE / LOWER / NO_CHANGE; quotes never loosen; proposal only (`applies: false`) with a replay of its effect | RV on constructed cases; no OBSERVED input yet | `research/learner/cost_retune.py`, decision 0007 |
| Registered hypothesis for the retune | PRE_REGISTERED | `H-A-COST-QUANTILE` in `research/registry/hypotheses.yaml` |
| Operator tool: `drill` (CI), `retune`, `status` | RV (drill) | `tools/learn.py` |

Run the drill: `python tools/learn.py drill`. With a shadow record on file: `python tools/learn.py retune --history data/live`.

## P9 Drift monitors and learning on screen

| Deliverable | Status | Where |
|---|---|---|
| PSI drift monitors per input feature (30 days vs the 180 before); FAIL renders ABSTAIN in the regime layer (`DRIFT`) | RV on constructed and FIXTURE series | `research/learner/drift.py`, `engine/regime/ccmrm.py`, decision 0008 |
| Calibration expiry (30 days); missing or expired is non-authoritative | RV | `drift.calibration_status` |
| Learner budget halts on OBSERVED drift or calibration FAIL: the registry refuses new hypotheses | RV | `registry.register(..., halted=)` |
| Data screen: drift board replaces "not run" | RV | `drift_board` in `engine/bff/projections.py` |
| Bots & Models: learning outcomes (episodes, process-error rate, training set) and learner proposals vs live parameters, no Apply control | RV | `learning` in `engine/bff/projections.py` |
| Operator tool: `tools/learn.py drift --history data/live` | RV | `tools/learn.py` |
