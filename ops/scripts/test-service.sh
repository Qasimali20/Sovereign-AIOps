#!/usr/bin/env bash
# The designated test service (ops/deploy/test-service): the ONLY thing the platform may act on.
# Thin wrapper over backend.incidents.testsvc so manual use and the platform share one deployment path and history.
#   ops/scripts/test-service.sh up        build images if missing, deploy v1 (one replica)
#   ops/scripts/test-service.sh break     stop it (simulates "service health check fails")
#   ops/scripts/test-service.sh status    release, rollback target, replicas, recent history
#   ops/scripts/test-service.sh down      remove the primary and any replicas
set -euo pipefail
cd "$(dirname "$0")/../.."
PY=.venv/bin/python
case "${1:-up}" in
  up)
    docker image inspect aiops-test-svc:v1 aiops-test-svc:v2 >/dev/null 2>&1 || ops/deploy/test-service/build.sh
    $PY -c "from backend.incidents import testsvc; print(testsvc.deploy('v1', 'operator', 'manual deploy'))"
    sleep 2; curl -sf http://127.0.0.1:18080/health && echo "aiops-test-svc healthy" ;;
  break)  docker stop -t 2 aiops-test-svc >/dev/null && echo "aiops-test-svc stopped" ;;
  status) $PY -c "import json; from backend.incidents import testsvc; print(json.dumps(testsvc.status(), indent=1))" ;;
  down)   docker ps -aq --filter label=aiops.test=true | xargs -r docker rm -f >/dev/null && echo "test service removed" ;;
  *) echo "usage: $0 up|break|status|down"; exit 1 ;;
esac
