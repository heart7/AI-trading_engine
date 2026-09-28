# UCHFE — Unified Crypto Hedge Fund Engine

Capital-tiered spot + perpetual trend-following engine, built from the UCHFE Production Engineering Prompt v10.4.

Engineering only. Not investment, legal, tax or regulatory advice, and not a claim of profitability.
The engine runs in PAPER mode by default; nothing in this repository places real orders.

## Layout (spec §13.3)
- `schemas/` JSON-Schema single source for claims, intents, events and the policy object
- `policy/` signed policy objects, enrolled signer public keys, approvals, ASSUMED register
- `engine/` decision, execution and evidence planes (built phase by phase)
- `research/` harness, Monte Carlo, hypothesis registry
- `tests/` unit, property and negative tests; `tests/negative/invariants.yaml` tracks all 43 invariants
- `tools/uchfe.py` operator CLI (enrol keys, sign, verify, coherence, activate)
- `docs/STATUS.md` four-way build status; `docs/OWNER-ACTIONS.md` what only the principal can do

## Run
```
python -m pip install -e ".[dev]"
python -m pytest -q tests
python tools/uchfe.py coherence --mode PAPER
```
