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
