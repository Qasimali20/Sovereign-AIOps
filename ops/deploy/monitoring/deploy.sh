#!/usr/bin/env bash
# Deploy or update the monitoring stack on DGX-3. Idempotent; safe to re-run after editing configs.
set -euo pipefail
cd "$(dirname "$0")"
# Fleet addresses come from the repository's .env (AIOPS_NODE_<n>="lan,tailscale"); the files here hold placeholders.
addr() { grep -m1 "^AIOPS_NODE_$1=" ../../../.env | cut -d= -f2 | cut -d, -f1; }
NODE2=$(addr 2); NODE3=$(addr 3)
[ -n "$NODE2" ] && [ -n "$NODE3" ] || { echo "AIOPS_NODE_2 / AIOPS_NODE_3 missing from .env" >&2; exit 1; }
HOST=${HOST:-tmc@$NODE3}   # LAN address: the Tailscale name can stall on Tailscale SSH re-authentication
DIR=/home/tmc/aiops-monitoring
python3 build_dashboards.py >/dev/null
STAGE=$(mktemp -d); trap 'rm -rf "$STAGE"' EXIT
rsync -a --exclude build_dashboards.py --exclude deploy.sh ./ "$STAGE/"
grep -rl NODE2_ADDR "$STAGE" | xargs -r sed -i "s/NODE2_ADDR/$NODE2/g"
ssh "$HOST" "mkdir -p $DIR"
rsync -a --delete "$STAGE/" "$HOST:$DIR/"
ssh "$HOST" "chmod 600 $DIR/.env && cd $DIR && docker compose up -d && \
  docker kill -s HUP aiops-prometheus >/dev/null && docker compose ps --format '{{.Name}} {{.Status}}'"
