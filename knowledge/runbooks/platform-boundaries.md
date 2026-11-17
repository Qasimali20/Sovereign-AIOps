---
always: true
---
# Platform boundaries

## Runtime and orchestration
Plain Docker and systemd on each node. There is NO Kubernetes, kubelet, liveness probe or container
orchestrator. A container stops only through the Docker daemon (an operator or script running
docker stop/kill/restart), its own exit, the kernel OOM killer, or its restart policy (shown as
restart_policy in inspect_container).

## What the platform may act on
Only registered test resources (currently aiops-test-svc on Node-2) may be acted on, and only after
policy evaluation and human approval. Application containers and systemd services on the real nodes
(Chambers, MIP/Rumaan, Langfuse, pension, livekit, vLLM units, speech services) are production. The
platform never acts on them: investigate, then escalate_to_human with concrete operator steps (for
example docker logs / docker inspect / systemctl status / journalctl -u for the unit). Everything else is
investigate-and-recommend only (autonomy level 2).
