#!/usr/bin/env bash
# Fill the dashboards with real serving metrics: light traffic to every model on the fleet.
#
#   ops/scripts/populate-metrics.sh            # 30 minutes
#   ops/scripts/populate-metrics.sh 60         # any number of minutes
#
# What it does, in order:
#   1. declares a maintenance window on all nodes for the run (+5 min), so alerts the traffic causes are
#      labelled "planned", not "real" (a genuine fault in that window is labelled planned too: keep runs short);
#   2. runs ops/bench/populate_metrics.py: one light client per model through the gateway (PaddleOCR over SSH),
#      pausing everything at 70 °C (GPU or CPU, read live every 5 s) until the fleet is back below 63 °C;
#   3. prints a per-model summary. Ctrl-C stops early and still prints it.
# Needs: .env with AIOPS_APPROVER_TOKEN, ops/bench/.loadkey (a gateway key), SSH to Node-1 for OCR.
set -euo pipefail
cd "$(dirname "$0")/../.."

MINUTES=${1:-30}
[[ $MINUTES =~ ^[0-9]+$ ]] && (( MINUTES >= 1 && MINUTES <= 180 )) || { echo "minutes must be 1-180" >&2; exit 2; }
[[ -f ops/bench/.loadkey ]] || { echo "ops/bench/.loadkey (gateway key) is missing" >&2; exit 2; }
API=${AIOPS_API:-http://127.0.0.1:5170}
TOKEN=$(grep -m1 '^AIOPS_APPROVER_TOKEN=' .env | cut -d= -f2-)
[[ -n $TOKEN ]] || { echo "AIOPS_APPROVER_TOKEN not found in .env" >&2; exit 2; }

body=$(printf '{"node":"*","minutes":%d,"reason":"Planned light traffic to every model to populate serving metrics (ops/scripts/populate-metrics.sh, %d min)"}' \
  "$((MINUTES + 5))" "$MINUTES")
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$API/api/maintenance" -H "x-approver-token: $TOKEN" \
  -H 'X-Requested-With: aiops' -H 'content-type: application/json' -d "$body")
[[ $code == 200 ]] || { echo "could not declare the maintenance window (HTTP $code); not sending traffic" >&2; exit 1; }
echo "maintenance window declared on all nodes for $((MINUTES + 5)) min"

exec .venv/bin/python -u ops/bench/populate_metrics.py ops/bench/.loadkey "$MINUTES"
