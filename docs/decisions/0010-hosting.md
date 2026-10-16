# 0010 — Hosting the engine on one London server, with a daily learning ledger

Status: requested by the principal on 2026-10-16 ("engine need to be hosted to storage that track learning").
PAPER only. Changes no policy value.

1. **Host.** One DigitalOcean Basic Droplet in London (LON1), Ubuntu 24.04, $6/month, with daily provider backups
   (+30%). Reasons: a UK region with clear pricing; Kraken and Bitstamp public endpoints are reachable
   directly, with no VPN or location masking (INV-43); a plain disk, so the hash-chained journals stay ordinary files;
   the engine needs about 80 MB of memory per replay, which fits the smallest size with a 1 GB swap file. Any Ubuntu
   server in the UK or EU works the same way: `deploy/install.sh` assumes nothing provider-specific.
2. **What is stored.** `/var/lib/uchfe` holds the certified history, the shadow journal, and the learning directory.
   All three are append-only and hash-chained (`AppendOnlyLog`). Nightly archives keep 14 days on the server; the
   provider's daily backups are the off-machine copy.
3. **Tracking learning over time.** `tools/learn.py snapshot` runs once a day: cost retune (decision 0007), PSI drift
   and regime calibration (decision 0008), episode status. It writes the current files the BFF reads, copies them to
   `snapshots/<date>/`, and appends one record to `learning/ledger.jsonl` with the content hash of each copy. The
   ledger is the learning trajectory; `tools/learn.py history` and the Bots & Models screen read it back.
4. **Regime calibration is now measured.** The snapshot computes §9.3 step 6 N1 (walk-forward ECE) on the certified
   store and writes `calibration.json`, so the 30-day expiry of decision 0008 runs on real measurements. FIXTURE
   sessions ignore it.
5. **Nothing self-applies.** Every proposal keeps `applies: false`. Code reaches the server only by fast-forward from
   `main`; policy changes still need the harness, a stress PASS and the principal's passkey signature.
6. **UI exposure.** The BFF binds to 127.0.0.1 and is reached through an SSH tunnel. The firewall allows SSH only.
7. **Real instrument ids.** The BFF now replays a certified live store (`--history`). It no longer assumes `FIXTURE_`
   names: `activity_universe`, `pair_rail` and the reporter resolve pairs by base asset. Figures from a live-store
   session are marked `fixture: false`; the replay itself is still a paper replay.
