#!/usr/bin/env bash
# Deploy the log collector to one or more nodes: ./deploy.sh Node-3 [Node-1 ...]  (default: all four)
set -euo pipefail
cd "$(dirname "$0")"
# Fleet addresses come from the repository's .env (AIOPS_NODE_<n>="lan,tailscale").
addr() { grep -m1 "^AIOPS_NODE_$1=" ../../../.env | cut -d= -f2 | cut -d, -f1; }
declare -A IP=([Node-1]=$(addr 1) [Node-2]=$(addr 2) [Node-3]=$(addr 3) [Node-4]=$(addr 4))
LOKI_HOST=${IP[Node-3]}
[ -n "$LOKI_HOST" ] || { echo "AIOPS_NODE_3 missing from .env" >&2; exit 1; }
PUSH=$(grep '^LOKI_PUSH_PASSWORD=' ../monitoring/.env | cut -d= -f2)
DIR=/home/tmc/aiops-logs
for n in "${@:-Node-1 Node-2 Node-3 Node-4}"; do
  for node in $n; do
    h="tmc@${IP[$node]}"
    ssh "$h" "mkdir -p $DIR && umask 077 && printf 'NODE_NAME=%s\nLOKI_HOST=%s\nLOKI_PUSH_PASSWORD=%s\n' '$node' '$LOKI_HOST' '$PUSH' > $DIR/.env"
    rsync -a config.alloy docker-compose.yml docker-compose.files.yml "$h:$DIR/"
    ssh "$h" "cd $DIR && f='-f docker-compose.yml' && { [ -d /home/tmc/Desktop/logs ] && f=\"\$f -f docker-compose.files.yml\" || true; } && \
      docker compose \$f up -d --force-recreate >/dev/null 2>&1 && docker ps --filter name=aiops-log-agent --format '$node {{.Status}}'"
  done
done
