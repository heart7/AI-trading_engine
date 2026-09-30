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
