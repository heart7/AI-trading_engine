# Owner actions for the P0 exit gate

The P0 gate (spec §19) needs three things only the principal can do. Everything else in P0 is built and tested.

## 1. Create two passkeys (main + backup)
No USB key is needed (decision 0009). Open the approval page on your phone or laptop:
https://heart7.github.io/AI-trading_engine/passkey/

Under "Create a passkey", give it a name (for example `phone`) and press the button. Your device asks for Face ID,
Touch ID, Windows Hello or its PIN. The page then shows one line of text, which is only the public part of the
passkey: paste it into the project thread and Claude enrols it. Do the same on a second device (for example
`laptop`) as the backup. If your passkeys sync through iCloud Keychain or Google Password Manager, make sure that
account has its own strong sign-in, because it now protects your approvals.

A FIDO2 USB or NFC key still works if you ever want one: `ssh-keygen -t ed25519-sk -O verify-required`, then send
the `.pub` line instead.

## 2. Sign policy v10.4.0 for PAPER
Claude sends you an approval request: a short block of text naming the action, the policy hash and a one-line
reason (yours, or one you agree with). Paste it under "Sign an approval" on the same page, check the details it
shows, and press "Check and sign". Approve with Face ID, Touch ID, Windows Hello or the PIN, then paste the result
back into the thread. The signature covers the policy hash and your reason, so neither can be changed afterwards.

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

## 5. Live-data record (P6): running
Since 2026-10-01 the record runs in the cloud every 4 hours on public Kraken and Bitstamp prices (no account, no API
key, no orders), with its journal in the project's shared `shadow` folder. Nothing to do. The commands below are
only for running it on your own computer as well.
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
```
Then sign it on the approval page as in step 2.

## 7. Before CANARY (P7), nothing needed yet
`python3 tools/golive.py` lists every go-live item and who owns it. The ones only you (or your accountant) can do:
- An accountant reviews `policy/tax/tax-uk-v1.yaml` and the matching report from `tools/tax_report.py`, fills in the
  reserve rate and annual exempt amount, and you record their sign-off in `policy/tax/signoff.json` with the config
  hash the checklist prints (§21 item 10b).
- An on-call rota in `policy/oncall.yaml` (§21 item 13). Days without cover put the engine in STOP.
- A Kraken access record (§8.6) and, at the end, your signature on the go-live policy hash.

