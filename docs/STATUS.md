# Build status (four-way, conduct rule 0.2.5)

Legend: **RV** implemented + run-verified · **IU** implemented, unverified · **SN** specified, not implemented · **UQ** unresolved design question.
"Run-verified" means a run record exists: CI uploads `runs/ci/tests.json` (command, environment hash, timestamps, output hash) and `runs/ci/invariants.json` on every push.

## P0 Foundations

| Deliverable | Status | Where |
|---|---|---|
| JSON-Schema single source: policy object, signal/admissibility/regime/sizing/tier claims, signal_intent, agent_activity_event, hypothesis, approval, run record | RV | `schemas/` |
| Policy object v10.4.0 (verbatim §20.1), YAML 1.2 number parsing, content hash | RV | `policy/policy-10.4.0.yaml`, `engine/policy/loader.py` |
| Coherence checks §20.2 (C01–C15) plus C16 drawdown rungs monotone | RV | `engine/policy/coherence.py` |
| Evidence checks C08/C13/C15 in PAPER | UQ | decision 0001 |
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
