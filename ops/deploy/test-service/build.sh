#!/usr/bin/env bash
# Build both releases of the test service (v1 good, v2 the known-bad release used by rollback drills).
set -euo pipefail
cd "$(dirname "$0")"
for v in v1 v2; do docker build -q --build-arg VERSION=$v -t aiops-test-svc:$v . ; done
