# Owner actions for the P0 exit gate

The P0 gate (spec §19) needs three things only the principal can do. Everything else in P0 is built and tested.

## 1. Enrol two hardware keys (primary + backup)
Any FIDO2 key that OpenSSH supports (YubiKey 5 series, SoloKey, Nitrokey 3). On your own computer, once per key:

```
ssh-keygen -t ed25519-sk -O verify-required -C "uchfe-primary" -f ~/.ssh/uchfe_primary
ssh-keygen -t ed25519-sk -O verify-required -C "uchfe-backup"  -f ~/.ssh/uchfe_backup
```
(Older keys without ed25519 support: use `-t ecdsa-sk`.) Then in the repo:
```
python3 tools/uchfe.py enroll ~/.ssh/uchfe_primary.pub --key-id primary
python3 tools/uchfe.py enroll ~/.ssh/uchfe_backup.pub  --key-id backup
```
Only public keys are stored. Software keys are refused. Keep the backup key somewhere separate.

## 2. Sign policy v10.4.0 for PAPER
```
python3 tools/uchfe.py statement --action POLICY_ACTIVATE --policy policy/policy-10.4.0.yaml \
    --rationale "Activate v10.4.0 for PAPER mode (P0)" -o stmt.json
ssh-keygen -Y sign -f ~/.ssh/uchfe_primary -n uchfe-approval@v1 stmt.json     # touch the key
python3 tools/uchfe.py attach stmt.json stmt.json.sig -o policy/approvals/10.4.0-paper.json
python3 tools/uchfe.py activate --mode PAPER policy/approvals/10.4.0-paper.json
```
The signature covers the policy hash and your written reason, so neither can be changed afterwards.

## 3. Answer the P0 open items (spec §21)
- Item 1: universe beyond BTC and XRP (ETH and SOL are the defaults; the spec suggests making ETH mandatory).
- Item 5: monthly operating-cost budget (currently ASSUMED at $200).
- Item 10: ask Binance and Bybit in writing whether the accounts may be used while you live in the UK.
- Decision 0001 (docs/decisions): how evidence-dependent coherence checks bind in PAPER.

## 4. For P3 (execution), when you are ready
Nothing here is needed for PAPER mode. None of it should be pasted into the chat or the repo.
- Open a UK Kraken account (spot, USD pairs) if you have not yet (spec §21 item 10a), and ask Kraken whether its demo environment covers spot so the conformance suite can run there.
- When a testnet run is wanted: create **read-only** and **trade-only** demo/testnet keys (never with withdrawal permission). They go into the secret store from Settings, not into this repo or the chat.
- Binance and Bybit stay data-only unless each confirms in writing that a UK resident may trade on the account (INV-43).

## 5. Start the live-data record (P6), when you want to
Uses public market data only: no account, no API key, no orders. Run it on your own computer, which can reach the
exchanges (this build's container cannot).
```
python3 tools/build_dataset.py --live --pair BTC --pair XRP --pair ETH --pair SOL --out data/live
python3 tools/shadow.py run --history data/live --journal runs/shadow/journal.jsonl
python3 tools/shadow.py status
```
Then schedule `run` a few minutes after each 4h close (cron `5 0,4,8,12,16,20 * * *`). Until §9.3 steps 1–5, 7, 8
pass on real history the cycles are recorded at the PAPER rung and do not count toward the 90 shadow days. Moving
to SHADOW needs your signed PROMOTE approval.

Once a few weeks of cycles have entries priced from the order book, you can check the cost model against them:
```
python3 tools/learn.py retune --journal runs/shadow/journal.jsonl --history data/live
```
It writes a proposal to `runs/learning/cost-proposal.json` and changes nothing (decision 0007).

## 6. Decision 0004 (venue cap), if you agree with the default
`policy/proposals/policy-10.4.1-venue-cap.yaml` lowers the per-venue cap from 40% to 20% so the "venue fails" stress
scenario stays inside the 20% drawdown budget. It passes the stress battery. It takes effect only if you sign it:
```
python3 tools/uchfe.py statement --action POLICY_ACTIVATE --policy policy/proposals/policy-10.4.1-venue-cap.yaml \
    --rationale "Lower venue cap to 20% until a second UK venue exists (decision 0004)" -o stmt.json
ssh-keygen -Y sign -f ~/.ssh/uchfe_primary -n uchfe-approval@v1 stmt.json
```

## 7. Before CANARY (P7), nothing needed yet
`python3 tools/golive.py` lists every go-live item and who owns it. The ones only you (or your accountant) can do:
- An accountant reviews `policy/tax/tax-uk-v1.yaml` and the matching report from `tools/tax_report.py`, fills in the
  reserve rate and annual exempt amount, and you record their sign-off in `policy/tax/signoff.json` with the config
  hash the checklist prints (§21 item 10b).
- An on-call rota in `policy/oncall.yaml` (§21 item 13). Days without cover put the engine in STOP.
- A Kraken access record (§8.6) and, at the end, your signature on the go-live policy hash.

