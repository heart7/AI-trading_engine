#!/usr/bin/env bash
# Set up (or repair) the hosted engine on a fresh Ubuntu 24.04 server. Idempotent: safe to run again.
# PAPER only. It reads no keys, places no orders and opens no inbound port except SSH.
#
#   curl -fsSL https://raw.githubusercontent.com/heart7/AI-trading_engine/main/deploy/install.sh | sudo bash
#
# Optional: put a state archive at /root/uchfe-seed.tar.gz first (deploy/README.md "Bring the existing record")
# to carry the current shadow journal and history over; otherwise the history is rebuilt from Kraken + Bitstamp.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run as root (sudo)"; exit 1; }
REPO_URL=${UCHFE_REPO_URL:-https://github.com/heart7/AI-trading_engine.git}
UCHFE_HOME=${UCHFE_HOME:-/opt/uchfe}; UCHFE_REPO=${UCHFE_REPO:-$UCHFE_HOME/repo}; UCHFE_STATE=${UCHFE_STATE:-/var/lib/uchfe}
UCHFE_BRANCH=${UCHFE_BRANCH:-main}

echo "== packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq git python3 python3-venv python3-pip ufw ca-certificates curl >/dev/null

echo "== swap (1 GB, for the smallest droplet)"
if ! swapon --show | grep -q /swapfile; then
  [ -f /swapfile ] || { fallocate -l 1G /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null; }
  swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

echo "== firewall: SSH only (the UI listens on 127.0.0.1 and is reached through an SSH tunnel)"
ufw allow OpenSSH >/dev/null
ufw --force enable >/dev/null

echo "== user and folders"
id uchfe >/dev/null 2>&1 || useradd --system --home "$UCHFE_STATE" --shell /usr/sbin/nologin uchfe
mkdir -p "$UCHFE_HOME" "$UCHFE_STATE"/{history,shadow,learning,backups}

echo "== code ($UCHFE_BRANCH)"
if [ -d "$UCHFE_REPO/.git" ]; then
  git -C "$UCHFE_REPO" fetch -q origin "$UCHFE_BRANCH" && git -C "$UCHFE_REPO" checkout -q "$UCHFE_BRANCH" \
    && git -C "$UCHFE_REPO" merge -q --ff-only "origin/$UCHFE_BRANCH"
else
  git clone -q --branch "$UCHFE_BRANCH" "$REPO_URL" "$UCHFE_REPO"
fi
git config --system --add safe.directory "$UCHFE_REPO" 2>/dev/null || true
. "$UCHFE_REPO/deploy/bin/uchfe-env"
[ -x "$PY" ] || python3 -m venv "$UCHFE_VENV"
"$UCHFE_VENV/bin/pip" install -q --upgrade pip
"$UCHFE_VENV/bin/pip" install -q -e "$UCHFE_REPO"

if [ -f /root/uchfe-seed.tar.gz ] && [ ! -s "$JOURNAL" ]; then
  echo "== importing /root/uchfe-seed.tar.gz"
  tar -C "$UCHFE_STATE" -xzf /root/uchfe-seed.tar.gz
  mv /root/uchfe-seed.tar.gz /root/uchfe-seed.imported.tar.gz
fi
chown -R uchfe:uchfe "$UCHFE_STATE"

echo "== reachability (public endpoints only; no VPN or location masking, INV-43)"
for u in https://api.kraken.com/0/public/Time https://www.bitstamp.net/api/v2/ticker/btcusd/; do
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "$u" || echo 000)
  echo "   $u -> $code"
  [ "$code" = 200 ] || echo "   WARNING: not reachable from this region; the shadow record needs Kraken and Bitstamp"
done

if ! ls "$HISTORY"/*-USD.bars.jsonl >/dev/null 2>&1; then
  echo "== building certified history (400 days; older bars Bitstamp-only and flagged single-source)"
  cd "$UCHFE_STATE"
  for b in "${PAIRS[@]}"; do
    sudo -u uchfe "$PY" "$UCHFE_REPO/tools/build_dataset.py" --live --pair "$b" --days 400 --allow-single-source --out "$HISTORY"
  done
fi

echo "== services and timers"
install -m 0644 "$UCHFE_REPO"/deploy/systemd/*.service "$UCHFE_REPO"/deploy/systemd/*.timer /etc/systemd/system/
ln -sf "$UCHFE_REPO/deploy/bin/uchfe-status" /usr/local/bin/uchfe-status
systemctl daemon-reload
systemctl enable --now uchfe-cycle.timer uchfe-learn.timer uchfe-backup.timer uchfe-update.timer >/dev/null
systemctl start uchfe-cycle.service || true      # records a cycle now if one is due
systemctl start uchfe-learn.service || true      # first learning snapshot
systemctl enable --now uchfe-ui.service >/dev/null
systemctl restart uchfe-ui.service

echo
uchfe-status || true
echo
echo "Done. View the UI from your computer:  ssh -N -L 8710:127.0.0.1:8710 root@<server-ip>  then open http://localhost:8710/"
