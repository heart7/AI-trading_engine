# Hosting the engine (paper only)

One small Linux server runs the shadow record every 4 hours, writes a learning snapshot every day, keeps everything
on disk, and serves the read-only UI to you over an SSH tunnel. It reads no keys and places no orders. Decision 0010
explains the choice.

## What runs, and when (UTC)

| Job | When | What it writes |
|---|---|---|
| `uchfe-cycle` | 00:10, 04:10, 08:10 … | tops up `history/` from Kraken + Bitstamp, appends one cycle to `shadow/journal.jsonl` |
| `uchfe-learn` | 00:40 daily | `learning/`: cost proposal, drift, regime calibration, one row in `ledger.jsonl`, copies in `snapshots/<date>/` |
| `uchfe-backup` | 01:15 daily | `backups/uchfe-state-<time>.tar.gz` (last 14 kept) |
| `uchfe-update` | 02:30 daily | fast-forwards the code to `main` (only what you merged), restarts the UI |
| `uchfe-ui` | always | the engine UI on `127.0.0.1:8710`, rebuilt after each cycle |

Everything the engine has recorded or learned lives in `/var/lib/uchfe`:

```
/var/lib/uchfe/
  history/<PAIR>-USD.bars.jsonl     certified 4h bars (hash-chained)
  shadow/journal.jsonl              one record per 4h cycle (hash-chained)
  learning/ledger.jsonl             one record per day of what the learner measured and proposed (hash-chained)
  learning/snapshots/<date>/        the proposal, drift and calibration files behind that day's record
  backups/                          nightly archives of all of the above
```

Nothing in `learning/` changes the engine. A proposal says `applies: false`; making one live is a policy change
with the harness, a stress PASS and your passkey signature (decisions 0007, 0008).

## Set it up (about 15 minutes, once)

1. **Create a DigitalOcean account** at digitalocean.com and add a payment card.
2. **Create a Droplet**: Ubuntu 24.04 LTS, region **London (LON1)**, Basic, Regular, the **$6/month** size.
   - Authentication: **SSH key** (add your computer's public key; the page shows how).
   - Backups: turn on **daily backups** (+30%, about $1.80/month). This is the off-server copy.
   - Advanced options › **Add initialization scripts (user data)**: paste the contents of `deploy/cloud-init.yaml`.
3. Wait about 10 minutes after it starts. The first run builds 400 days of history from Kraken and Bitstamp.
4. Check it from your computer:
   ```
   ssh root@<droplet-ip> uchfe-status
   ```
   You should see four timers, `journal: N cycles, hash chain intact` and a learning ledger row.

Without user data, log in and run the installer by hand:
```
ssh root@<droplet-ip>
curl -fsSL https://raw.githubusercontent.com/heart7/AI-trading_engine/main/deploy/install.sh | bash
```
The installer is safe to run again; it repairs and never deletes recorded data.

## Look at the UI

The UI only listens on the server itself, so nothing is exposed to the internet. Open a tunnel from your computer
and leave it running:
```
ssh -N -L 8710:127.0.0.1:8710 root@<droplet-ip>
```
then open http://localhost:8710/. **Bots & Models › Learning over time** shows the daily ledger.

## Bring the existing record (optional)

The record started in the cloud on 2026-10-01 can move with you, so the journal stays one unbroken chain.
Before step 2, copy the archive Claude prepared to the new server and run the installer:
```
scp uchfe-seed.tar.gz root@<droplet-ip>:/root/uchfe-seed.tar.gz
ssh root@<droplet-ip> "curl -fsSL https://raw.githubusercontent.com/heart7/AI-trading_engine/main/deploy/install.sh | bash"
```
The installer imports it only when the server has no journal yet. After that, the cloud routine should stop so the
two records don't diverge.

## Everyday commands (on the server)

```
uchfe-status                                        # health, last runs, journal, ledger, disk, backups
journalctl -u uchfe-cycle -n 50                     # last cycle's output
/opt/uchfe/repo/deploy/bin/uchfe-learn --force      # take another learning snapshot today
sudo -u uchfe /opt/uchfe/venv/bin/python /opt/uchfe/repo/tools/learn.py history --dir /var/lib/uchfe/learning --limit 90
```

Restore from a nightly archive: `systemctl stop uchfe-cycle.timer uchfe-learn.timer`, then
`tar -C /var/lib/uchfe -xzf /var/lib/uchfe/backups/<file>.tar.gz`, then start the timers again.

## Rules this keeps

- PAPER only. There are no keys on the server and nothing in `deploy/` reads one.
- No VPN or location masking (INV-43). The server sits in London and talks to Kraken and Bitstamp directly. Binance
  and Bybit refuse some regions; the feed skips any venue that refuses and names it in the journal.
- Code changes reach the server only through `main`, which only you merge.
- Nothing personal is stored in this repository or on the server: the server holds public prices and the engine's
  own records.
