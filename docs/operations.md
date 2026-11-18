# Operations

Deploying, upgrading, running and recovering the platform.

**Contents**

- [Platform reference](#platform-reference)
- [Deployment](#deployment)
- [Day-to-day operation](#day-to-day-operation)

## Platform reference

The full operator's reference for the platform as deployed on the DGX Spark fleet.

One console for operating the 4× DGX Spark fleet (Node-1..4) and the AI running on it:

- **AIOps:** incidents detected by deterministic rules, investigated by an agent, fixed only after a human
  approves, independently verified, and recorded in a tamper-evident audit trail. Also fleet, services and
  security (SOC) monitoring.
- **LLMOps:** an OpenAI-compatible model gateway, a log of every request through it (metadata only),
  GPU energy per model, quality evaluations of every model, and a registry of model versions.
- **AgentOps:** every investigation the agent ran, step by step, with each model call and approvers'
  verdicts on its diagnoses.

Everything runs on the fleet against self-hosted models (console and gateway on Node-2, monitoring on
Node-3). Nothing leaves the cluster. The incident pipeline:

```
Spark Monitor agents :8765 (x4, read-only) ─┐
docker events (lifecycle recorder) ─────────┤
test service health check ──────────────────┴─> detector (rules, no LLM)
                                                   │ incident
                                                   ▼
             LangGraph: collect_evidence -> retrieve runbooks -> diagnose (-> more evidence, max 2) -> plan
                        -> policy engine (deterministic)
                             ├─ allow / deny ─────────────────────────────> report
                             └─ requires_approval -> human approval (interrupt)
                                    └─ approved -> restricted executor -> verify -> report
```

### Codebase

```
sovereign-ai-ops/
├── backend/        all server code (Python: FastAPI + LangGraph)
├── frontend/       the web console (React)
├── tests/          backend tests
├── ops/            tooling: deploy, scripts, benchmarks, evaluation scenarios
├── knowledge/      runbooks the agent reads, CVE catalogue
├── docs/           architecture.md, operations.md, evaluation.md
└── data/           runtime state (databases, keys, backups); not in the repo
```

**`backend/`**, grouped by job. Entry points: `backend.web:app` (console + API, :5170),
`backend.llmops.gateway:app` (model gateway, :8790), `python -m backend.core.backup` (nightly backup).

```
backend/
  core/           shared foundation
    config.py       fleet, model endpoints, actionable resources, thresholds, rate limits
    store.py        SQLite (WAL) store: incidents, evidence, approvals, hash-chained audit
    analytics.py    read-only dashboard and pipeline aggregates
    llm.py          the agent's model client: health-checked routing, fallback, validated JSON
    backup.py       nightly backup, restore test, copy to Node-3
    ratelimit.py    token buckets (per IP on /api, per API key on the gateway)
    evals.py        evaluation records (data/evaluations.jsonl)
  incidents/      AIOps incident pipeline
    detector.py     deterministic alert rules (no LLM)
    runtime.py      background loop: detector, event recorder, workflow runner, recovery
    graph.py        the LangGraph workflow (typed state, bounded loops, approval interrupt)
    tools.py        read-only evidence tools
    knowledge.py    runbook retrieval (RAG): embed + rerank, keyword fallback
    policy.py       deterministic policy engine (operations, targets, parameter allowlist)
    executor.py     restricted executor (re-checks approval and policy)
    verify.py       independent recovery verification
    testsvc.py      the test service's deployments: deploy, rollback, scale, clear cache, restart
  monitoring/     what the fleet is doing
    fleet.py        cached snapshot of all four nodes for the UI
    engines.py      model servers Spark Monitor does not see (SGLang, Ollama, vLLM-Omni)
    services.py     containers and systemd units per node, transition alerts
    kernel.py       GPU driver (NVRM Xid) events + Xid catalogue
    events.py       container lifecycle recorder
    metrics.py      Prometheus exporter        promql.py, boards.py   history and charts from Prometheus
    logs.py         Loki log search for investigations
  llmops/         LLMOps
    gateway/        OpenAI-compatible model gateway (separate service): discovery, breaker, API keys
    requestlog.py   gateway request records, metadata only, 7-day retention
    summary.py      request summaries and GPU energy per model
    model_evals.py  quality evaluations every 6 h (fixed checks, no judge model)
    model_registry.py  model identity per server: track and compare only
  agentops/
    runs.py         agent runs step by step, diagnosis verdicts
  security/       SOC and users
    events.py, rules.py, attack.py   auth telemetry, detection rules, ATT&CK mapping
    vulns.py        offline CVE relevance          lab.py, tools.py   the SOC lab and its responses
    auth.py         console users (scrypt), sessions, roles viewer < approver < admin
  web/            HTTP layer
    __init__.py     app factory, router wiring, serves the built frontend
    security.py     network allowlist, CSRF guard, CSP, login throttle
    routes/         one file per API area: incidents, infra, gateway, llmops, agentops, soc, governance, users, auth
```

**Everything else**

```
frontend/src/     pages/ (one per screen), components/, lib/ (API client, queries, formatting);
                  tests/smoke.mjs opens every page in a real browser
tests/            317 tests (policy, executor, HTTP security, gateway, detector, SOC, recovery, audit chain, ...);
                  test_contracts.py checks live Prometheus, Loki and the gateway (skipped when unreachable)
ops/deploy/       systemd units, test-service images, monitoring stack (Prometheus, Grafana, Loki on Node-3),
                  log-shipping agent per node
ops/scripts/      aiops.py (CLI), users.py, populate-metrics.sh, test-service.sh, demo_snapshot.py
ops/bench/        populate_metrics.py, inference and failover benchmarks; results/ holds their reports
ops/evaluation/   evaluation scenarios, incident replay, gateway benchmark, time-to-fix harness
```

### Web console

React (Vite + TypeScript) in `frontend/`, built to `frontend/dist`, and served by the API on port **5170**.

| From | URL |
|---|---|
| Office LAN | http://<node-2-ip>:5170 |
| Tailscale | http://<node-2-tailscale-ip>:5170 |
| API docs | `/api/docs` (login required) |

Sections: Dashboard (key figures, incident flow, governance, activity), Incidents (search, filters,
sorting, paging) and incident detail (pipeline, approval, diagnosis with evidence links, evidence,
audit trail, raw record, close with note), Approvals, Security (SOC), Fleet (live tiles, rings and
history per node, one time range for the page) and per-node history, Services, Models (tabs:
Servers, Requests, Evaluations, Registry), Governance (tabs: Audit log with filter + CSV export,
Policies & guardrails, Agent runs with per-step model calls and diagnosis verdicts), Users (admin),
Account (password change).

Where AIOps, LLMOps and AgentOps live: AIOps is Incidents, Approvals, Security, Fleet and Services;
LLMOps is Models (servers, gateway requests and GPU energy, quality evaluations, model registry) and
Metric history; AgentOps is Governance → Agent runs (each investigation step by step, with approvers'
verdicts giving the diagnosis accuracy).

**Access control:** a client IP allowlist (localhost, `<office-lan>`, Tailscale `100.64.0.0/10`;
override with `AIOPS_ALLOWED_NETS`), then a login (scrypt-hashed passwords, signed HttpOnly
SameSite=Strict cookie, 12 h), with roles `viewer` < `approver` < `admin`. POSTs need `X-Requested-With`.
Login is rate-limited (5 failures per 5 min per IP). Approvals are recorded under the logged-in user.

```bash
uv run ops/scripts/users.py add NAME approver          # prompts for a password (run in a real terminal)
uv run ops/scripts/users.py add NAME viewer --generate # prints a generated password once
uv run ops/scripts/users.py list | delete NAME
```

### Run

Runs as a systemd **user** service (lingering is enabled, so it starts at boot):

```bash
systemctl --user status aiops          # ~/.config/systemd/user/aiops.service
systemctl --user restart aiops
journalctl --user -u aiops -f
cd frontend && npm run build           # after UI changes, then restart is not needed (static files)
.venv/bin/python -m pytest tests -q    # 317 tests; a full run records itself as an evaluation
ops/scripts/populate-metrics.sh 30     # light traffic to every model for 30 min, so the charts fill with real data
                                       # (declares a maintenance window first; pauses at 70 °C GPU or CPU, read live)
cd frontend && SMOKE_PASS=… npm run test:smoke   # sign in and open every page in a real browser
.venv/bin/python ops/scripts/demo_snapshot.py && (cd frontend && npm run build:demo)
                                       # offline demo: the real console on a recorded snapshot, one HTML file
                                       # (frontend/dist-demo/sovereign-ai-ops-demo.html), read-only, no network
systemctl --user list-timers aiops-backup.timer   # nightly backup with restore test (02:30)
ops/deploy/test-service/build.sh       # build test-service releases v1 (good) and v2 (bad)
```

### Demo: "test service becomes unhealthy" (blueprint section 15)

`ops/scripts/test-service.sh break`. An incident appears in the console within about 20 s and moves to
**Awaiting approval**. Open it, review the evidence and proposal, then **Approve & execute** or
**Reject**. After approval the executor restarts the container and the verifier confirms recovery
with independent health checks. The CLI (`ops/scripts/aiops.py`, uses `AIOPS_APPROVER_TOKEN` from
`.env`) does the same from a terminal.

The test service (`ops/deploy/test-service`) has four real failure modes, each with its own fix:
a bad release (v2, fixed by `rollback_test_deployment`), a full cache (`clear_test_cache`),
demand above replica capacity (`scale_test_service`, 1..3 replicas) and a stopped container
(`restart_test_service`). `ops/evaluation/scenarios/sre_test_service.py` drives all four end to end
(latest: 16/16 hard, 16/16 quality). If the problem clears before anyone approves, the approval
expires and the incident closes as **Recovered**.

### Autonomy

Level 3 (approved execution) applies **only** to `aiops-test-svc` and to the SOC lab's simulated
state. Every other incident, including real model servers on all four nodes, is level 2:
investigate and recommend `escalate_to_human` with manual steps. The executor runs only fixed
Docker commands from `backend/incidents/testsvc.py` against the registered test service (restart, clear
cache, scale to 1..3 replicas, roll back to the previous good release) and lab-state changes
bound to the incident's subject.

### Known limits
- SQLite instead of Postgres (ADR 0003); no Langfuse tracing yet (the audit trail is in SQLite,
  hash-chained).
- **Plain HTTP by decision (2026-11-14).** Traffic over Tailscale is encrypted by WireGuard; on the office
  Wi-Fi, logins and API keys travel unencrypted. If that changes: enable HTTPS certificates for the tailnet,
  or serve TLS from uvicorn with an internal CA.
- The detector's "model missing" baseline starts empty on every API restart.
- Guided JSON decoding on the current vLLM build runs away emitting whitespace on enums, so output
  is validated in code instead.

### GPU failure detection (Phase 3)

The detector reads `NVRM: Xid` events from each node's kernel journal (locally on Node-2, over SSH
elsewhere). Each Xid code becomes one incident with its severity, the processes involved, the count of
`bad register read` errors and operator guidance. Xid 119/154 (GSP hang, reset required) are critical,
and the agent is told that a service restart does not fix them; real nodes stay recommend-only.

Coverage: all four nodes. Node-2 is read locally; the others over SSH to their Tailscale names
(`spark-1/3/4`) first, falling back to the LAN IP. The working path is remembered. Node-4 only accepts
Tailscale SSH. The Fleet page shows per-node coverage and flags any node that becomes unreadable.

Other detector rules (thresholds in `backend/core/config.py`), each held for about 3 minutes before it fires:
- `high_cpu`: ≥ 90%.
- `gpu_stalled` / `model_queue_stuck`: GPU at 0% with requests in flight, or requests waiting with none
  running, **while the server completes no request**. Queue counts are compared with the server's resting
  level, because vLLM-Omni (the TTS server on Node-4) leaves finished requests counted after busy periods;
  those leftovers caused repeated false alarms until 2026-11-17.
- `model_latency_high`: chat-model first-token p95 above 10 s with at least 2 requests a minute arriving.
  Below that rate the p95 is one or two requests, often the agent's own long diagnosis prompts.
- `node_rebooted` (uptime went backwards), and a 15-minute post-boot grace before `model_server_missing`.

Evaluation against the real 2026-11-13 hang:
```bash
uv run ops/evaluation/scenarios/gpu_hang_replay.py        # scratch DB; 5/5 checks pass
uv run ops/evaluation/replay.py INC-... --runs 3 --forbid kubernetes --expect stop
```

### Model gateway (Phase 2)

`aiops-gateway.service` (user unit) serves an OpenAI-compatible API at `http://<node-2-ip>:8790/v1`
(Tailscale `<node-2-tailscale-ip>`). Every model server on the four nodes is discovered from the Spark Monitor
agents and probed from here; only servers that answer become routable. Aliases: `chat` (falls back to
Node-3's Qwen3-30B when it is reachable), `embed`, `rerank` (single model only: vectors from another model
would corrupt indexes), `asr`, `tts`. API keys are managed by admins through the API (`GET/POST /api/gateway/keys`, `POST /api/gateway/keys/{id}/revoke`, see `/api/docs`); the console has no key page at present.

Resilience: active probes every 15 s; passive breaker on connect errors and 502/503/504 with exponential
backoff and a half-open single trial; retries only before the first byte; per-replica concurrency
limit with bounded queueing. The SRE agent itself calls the gateway (alias `chat`), with a direct path
to Node-2 as a last resort.

Evidence:
- `ops/evaluation/scenarios/gateway_failover.py`: a broken replica (HTTP 503, then dropped connections)
  next to the real one; 48/48 requests succeed, broken replica backed off (8/8 checks).
- `ops/evaluation/bench_gateway.py`: at 32 concurrent streams, gateway vs direct is −0.3% throughput and
  +12 ms TTFT p50 (uvloop + httptools). See Gateway overhead benchmark in `docs/evaluation.md`.

**Second LLM replica (2026-11-14):** Node-3's `qwen3-30b` container (Qwen3-30B-A3B-Instruct-2507 FP8,
served as `qwen-chat`, `--max-num-seqs 4`) was recreated from its exact definition (image pinned by
digest) with an added `<node-3-ip>:8103` binding. It keeps `127.0.0.1:8103` for MIP/Rumaan, which uses
host networking. The original container is kept stopped as `qwen3-30b-pre-aiops`; backup and script are in
`~/aiops-backup/` on Node-3. The gateway caps it at 4 in flight (`GATEWAY_MAX_INFLIGHT_BY_MODEL`).
`ops/evaluation/scenarios/gateway_cross_node_failover.py`: primary down, so 12/12 `chat` requests are served by Node-3.

Still not routable: Node-3 `qwen-embed`/`qwen-rerank` (intentionally: different vector space) and
Node-1 PaddleOCR (port blocked). Aliases can be overridden with `AIOPS_GATEWAY_ALIASES` (JSON).

### LLMOps and AgentOps

All under **Models** (tabs) and **Governance → Agent runs** in the console:
- **Requests** (`backend/llmops/requestlog.py`, `backend/llmops/summary.py`): every gateway request with client, model, node,
  latency, tokens and outcome; metadata only, never prompt or answer text; 7-day retention. GPU energy per
  model is an estimate: power above each node's idle draw, shared by request time.
- **Evaluations** (`backend/llmops/model_evals.py`): every model every 6 hours on small fixed test sets, scored by
  exact checks (no judge model): chat answers, retrieval for embeddings and reranker, word error rate for
  speech, character error rate for OCR. A score at least 25 points under the model's own recent level and
  under 75% opens a `quality_regression` incident; it clears when a later run is back to normal.
- **Registry** (`backend/llmops/model_registry.py`): each server's identity (engine version, model path, context
  length, weights digest), when it changed, restarts, and evaluation scores before and after a change.
  Track and compare only: nothing is deployed or rolled back from here.
- **Agent runs** (`backend/agentops/runs.py`): each investigation step by step with its model calls (time,
  tokens, retries, fallbacks). Approvers rate each diagnosis Correct / Partly right / Wrong, which gives the
  diagnosis accuracy figure.

### Security testing: prompt injection (Phase 7)
See Prompt-injection assessment in `docs/architecture.md`. Deterministic tests with a hijacked fake model
(`tests/test_injection.py`) plus a live evaluation of 5 attacks planted in container logs
(`ops/evaluation/scenarios/prompt_injection.py`): every hard guarantee held, model resistance was 10/10,
and the model flagged the attack in 5/5 cases. Run the live evaluation after any model, prompt or tool change.

### UI theme
Dark slate navy base (`#132036` sidebar, links, marks; dark mode surfaces built on it) with TMC lime (`#84BD00`) as a sparing accent:
primary buttons, the brand mark and the final "verified recovered" outcome only. Emphasis panels are
light navy tints, never solid dark slabs; attention counts are amber. Deliberately typographic: no
icon circles on KPI tiles, no icons before card titles or pipeline stages (icons only where they carry
meaning, e.g. status badges). Red Hat Display / Open Sans (self-hosted), 2px corners, no shadows.
Model-kind tags use the Spark Monitor hues. Key-figure tiles and the Fleet rings follow Spark Monitor's
look and animation; in charts GPU is light blue and CPU red.

### Service monitoring
Every scan reads `docker ps -a` and `systemctl list-units` on all nodes (read-only). Alerts: a container
turns unhealthy, loops restarting, or stops after having been seen running (once per transition); a
systemd unit fails or crash-loops. Crash loops are caught from each unit's restart counter (`NRestarts`), not
only from its state, because a looping unit spends most of its time starting; the incident clears only after the
unit has stayed up with an unchanged counter for 5 minutes, so one crash loop is one incident. Unit incidents
include the unit's own log as evidence. `iwpmd.service` (iWARP port mapper, no iWARP NIC) is known-benign and
shown, never alerted. Production services are investigate-and-recommend only. Node-1 already runs Prometheus
and Grafana containers.

### Disk and network monitoring
Each scan also reads, on every node over the same SSH connection, `/proc/diskstats`, `/proc/net/dev`, the
link state of the physical interfaces, the Wi-Fi station counters (`iw`) and storage/NIC kernel messages
(`backend/monitoring/hostio.py`). All four nodes reach the office network over Wi-Fi (`wlP9s9`); the wired
port `enP7s7` is unplugged; the ConnectX ports `enp1s0f1np1` and `enP2p1s0f1np1` are the direct node-to-node
links. Rules: `disk_io_saturated` (drive 95% busy for 15 min), `disk_error` (NVMe timeout or reset, I/O
error, filesystem error or read-only remount), `net_errors` (interface errors rising for 3 scans),
`net_link_down` (an interface seen up goes down), `net_nic_reset` (NIC watchdog) and `net_wifi_degraded`
(signal at or below -75 dBm for 3 min). Kernel-message incidents are closed by a person; the others clear
when a fresh reading shows them gone. Series: `aiops_disk_*`, `aiops_network_*`, `aiops_wifi_*` (Fleet
dashboard, "Storage and network health").

### Runbook retrieval
The agent reads versioned runbooks from `knowledge/runbooks/*.md`. Each `## ` section is a chunk
(`file#slug`, versioned by the file's content hash); `always: true` documents go into every prompt.
Retrieval uses the gateway's `embed` then `rerank` aliases, falling back to keyword overlap (with a
60 s back-off) when the models are unreachable. Runbooks go into a separate `<runbooks>` prompt block
and can never be cited as evidence. Each incident records a `retrieve_runbooks` step and lists the
sections and versions it saw under `knowledge` in its report. See `docs/architecture.md` §5.
Each hypothesis names the runbook sections that informed it in `runbook_ids`, restricted by the response
schema to the sections retrieved for that incident. AgentOps (Agent runs) shows retrieval method, latency,
fallback share, how often diagnoses used a retrieved section, the accuracy of rated diagnoses with and
without one, and how often each section is retrieved and used; `aiops_rag_sections` and
`aiops_rag_embedding_ok` track the index.

### Governance and monitoring
- **Audit:** `audit_events` is a SHA-256 hash chain with append-only triggers, re-verified every
  10 minutes (`/api/audit/integrity`, metric `aiops_audit_chain_ok`, Prometheus alert `AuditChainBroken`).
- **Backups:** `aiops-backup.timer` (daily 02:30) backs up `data/`, restore-tests the archive
  (integrity check, audit chain, row counts), copies it to `~/aiops-backups` on Node-3 and keeps 14.
  Each run is recorded as the `backup-restore` evaluation (latest 7/7).
- **Rate limits:** token buckets per client IP on `/api` (20/s, burst 200) and per API key on the
  gateway (100/s, burst 500).
- **Quality figures** on the Dashboard: proposal acceptance, mean evidence support and the
  false-positive rate from the verdict chosen when an incident is closed.
- **Monitoring:** Prometheus and Grafana on Node-3 (`ops/deploy/monitoring`), 8 alert rules, dashboards
  Fleet, Model serving and AI operations. Loki on Node-3 collects logs from all four nodes; the agent
  searches it during investigations. The console shows node history from Prometheus and links to
  Grafana as "Metric history".

### Configuration drift
Every 5 minutes the platform reads each node's configuration over SSH (`backend/monitoring/drift.py`, read-only):
host versions (kernel, OS, NVIDIA driver, Docker, CUDA), every application systemd unit as `systemctl cat` prints it
(system units and the tmc user's units, drop-ins and enabled state included), every long-lived container (image and
image id, command, restart policy, ports, mounts, environment variable names with a hash of each value), and SSH
access (sshd configuration hash, authorized key fingerprints, sudoers.d file names). Environment values that look
like keys, tokens or passwords are masked before anything is stored; container environment values never leave the
node.

The first reading of a node is its baseline (`data/drift_baseline.json`, mode 600). Any difference opens a
`config_drift` incident per item with a diff; changes made inside a maintenance window are recorded as planned
work. The incident clears when the item is put back, or when an admin accepts the current configuration as the
new baseline on the Drift page (per item or all, with a reason, audited as `drift_accepted`). The Drift page
also lists host versions that differ between nodes; that is shown for information and is not drift.

### Controlled deployments
Changes to the fleet are made with Ansible playbooks (`ops/ansible`), run through one wrapper that previews,
records and verifies them. The inventory reads node addresses from `.env`; nothing about the fleet is stored in
the repository. Ansible is an optional dependency group: `uv run --group ops ...`.

| Playbook | Target | What it does | sudo |
|---|---|---|---|
| `facts` | all | read-only report of OS, kernel, driver, Docker, CUDA, memory and disk, and what differs between nodes | no |
| `log-agent` | all | the log collector: settings, configuration, recreated only when something changed | no |
| `monitoring` | Node-3 | Prometheus, Grafana, Loki: regenerates dashboards, restarts or reloads only what changed | no |
| `platform` | Node-2 | upgrade to the checked-out commit: refuses a dirty tree (Markdown edits excepted), runs the tests, builds the console, restarts the services, waits until they answer | no |
| `model-unit` | one node | changes one flag of a model server's systemd unit (for example `--gpu-memory-utilization`), restarts it, waits until it answers | yes |

```bash
uv run --group ops python ops/scripts/deploy.py log-agent --reason "roll out collector config"
uv run --group ops python ops/scripts/deploy.py model-unit --limit Node-4 --become --reason "TTS memory share" \
    -e unit=vllm-tts -e flag=--gpu-memory-utilization -e value=0.15 -e port=8091
```

The wrapper (1) runs the playbook with `--check --diff` and stops if nothing would change; (2) shows the preview
and asks for `yes`; (3) records the deployment through the API with the CLI token (audited as
`deployment_started`, with commit, nodes, reason and preview) and opens a maintenance window on the nodes it
changes; (4) applies it to those nodes only; (5) closes the record as succeeded or failed with the per-node recap
(`deployment_finished`) and re-reads the nodes' configuration, printing the drift the change caused.
`--accept-drift` accepts that drift as the new baseline with the deployment id as the reason. Deployments are
listed under Governance → Deployments. CI (GitHub Actions) only tests and builds; it never deploys.

**Private image registry: deferred (decision 2026-11-18).** Only the test service's image is built here, and it
is built on the node that runs it. A registry served over plain HTTP would need each node's Docker daemon
configured to allow it (sudo, and a Docker restart that stops every container on the node, including other
teams'). When a second image has to be shared between nodes, the planned approach is HTTPS on Node-3 with the
platform's own certificate installed per node under `/etc/docker/certs.d/`, which needs no Docker restart.

### Log strategy
One pipeline, one store, one place to search: every node runs the same collector (Grafana Alloy,
`ops/deploy/logs-agent`), which ships to Loki on Node-3. The console, the investigation agent and Grafana all
read the same Loki; nothing reads logs from the nodes directly.

| Source | Collected from | `component` label |
|---|---|---|
| systemd units, kernel, sshd, sudo | the journal (`/var/log/journal`, read-only) | `journal` |
| Docker containers | the Docker socket (read-only) | `container` |
| Units that write to their own files (Node-4: `vllm-asr`, `vllm-tts`, `vllm-whisper`, `vllm-qwen36`, `vllm-qwen38`, `omni`) | `~/Desktop/logs/*.log`, listed one by one in `config.alloy` | `file` |

**Labels** are kept low-cardinality: `node`, `environment`, `component`, `service`, `severity`. Request
ids, users and addresses stay in the line, never in a label.

**Masking before shipping:** passwords, tokens, API keys, `Authorization` headers and bearer tokens are
replaced with `<redacted>` on the node. Health-probe and metric-scrape access lines (`GET /metrics`,
`/health`, `/v1/models` with a 200) are dropped there too: they are most of the volume and carry no signal.

**Retention:** 14 days in Loki (about 50 MB at current volume). On a collector's first start it reads
back 12 hours of journal. Longer-term evidence lives elsewhere: incidents and their evidence records in
the platform database (backed up nightly, 14 copies), metric history in Prometheus, and the gateway's request
log (metadata only, never prompt or response text, 7 days).

**Access:** Loki listens on localhost on Node-3; the nginx gate on :3100 is the only way in, with separate
push (collectors) and query (platform, Grafana) credentials. When Loki cannot be read, investigations
record "log search unavailable" as evidence, never an empty result that reads as "no errors".

**Adding a source.** A service that logs to the journal or runs in Docker is collected without changes.
A service started with `StandardOutput=append:<file>` is invisible until its file is added to
`local.file_match "service_files"` in `config.alloy` with a `service` label equal to its unit name (without
`.service`). The agent finds a unit's logs by that name. Then run `ops/deploy/logs-agent/deploy.sh <node>`.
Such services are better switched to the journal: `journalctl` keeps them with the rest and
nothing needs listing. That change needs sudo on the node.

**Not collected:** application logs inside containers that write to files rather than stdout, and the
logs of the four nodes' desktop sessions.

### SOC (Phase 6)
Real `sshd`/`sudo` telemetry from all four nodes; rules for brute force, password spraying, login
after failures, sudo failures, known-malicious files (local IOC list) and beaconing; ATT&CK mapping
checked against a catalogue. Offline CVE relevance (`backend/security/vulns.py`) runs every 6 hours: a CVE
is `patched` if its id is in the package's Debian changelog, `not_affected` if the upstream version is
outside the range, otherwise `vulnerable` (an incident). On 2026-11-15 no node was vulnerable to the
five catalogued CVEs. Containment (`block_ip_lab`, `disable_lab_account`, `isolate_lab_endpoint`,
`quarantine_lab_file`) acts on the isolated lab only. `ops/evaluation/scenarios/soc_lab.py`: 7 cases,
28/28 hard, 25/26 triage quality (latest run; 26/26 the run before).

### Status against the blueprint

Status on 2026-11-17. The phase scopes below are as this repository uses them; see
`docs/evaluation.md` for measurements and Risk register in `docs/architecture.md` for open risks.

| Phase | Scope | Done | Remains |
|---|---|---|---|
| 0 | Inventory and network | Hardware, network and workload inventory (`docs/architecture.md`); SSH from Node-2 to all four nodes; port matrix (`docs/architecture.md`) | Wired Ethernet decision for Node-3/Node-4; network segmentation not done |
| 1 | Platform foundation | FastAPI + LangGraph platform on Node-2, React console, users and roles, SQLite store, systemd units, policy engine, restricted executor, independent verification, first vertical slice | PostgreSQL migration deferred by ADR 0003; secret store and rotation not done |
| 2 | Model gateway and LLMOps | OpenAI-compatible gateway with discovery, breaker, failover across Node-2 and Node-3, API keys, per-key rate limits; failover evaluations 8/8 and 3/3; request log, GPU energy, quality evaluations, model registry | Second replica of the primary model |
| 3 | Observability | Exporter, own Prometheus, Grafana and Loki on Node-3 (8 rules, 3 dashboards, logs from all nodes), GPU Xid detection on all four nodes, service monitoring with crash-loop detection, disk I/O, NIC errors, link state and Wi-Fi on every node, agent and runbook-retrieval measurement, 7/30-day history in the console | No Alertmanager; units logging to files must be listed in the collector |
| 4 | SRE agent | Detector rules incl. sustained CPU, GPU stall, latency, reboot; runbook RAG; self-recovery with approval expiry; GPU hang replay 5/5 | Time-to-fix measured for the AI-assisted arm only (median 92 s over 12 runs), manual arm not yet run; labelled scenarios for the remaining incident kinds |
| 5 | Controlled remediation | Versioned test service with four operations, parameter allowlist, deployment history and rollback; SRE drill 16/16 hard, 16/16 quality | Level 3 stays limited to the test service by design |
| 6 | SOC | Auth telemetry, 7 rules, ATT&CK catalogue, offline CVE relevance, lab with 6 scenarios and 4 lab-only responses; SOC lab 28/28 hard, 25-26/26 quality | Wazuh/SIEM integration (needs sudo on the nodes and an install decision); precision/recall measured only on 7 lab scenarios |
| 7 | Security testing and governance | Prompt-injection tests and live evaluation (20/20); authorisation-bypass tests; hash-chained audit; nightly backups with restore test; rate limits; data files mode 600; 333 tests | Network segmentation and secret rotation; TLS declined by decision; Prometheus read API open on the LAN |


## Deployment

How to deploy, upgrade and restart the platform reproducibly. All commands run as `tmc`.
Paths are relative to `~/sovereign-ai-ops` on Node-2 unless stated otherwise.

### 1. What runs where

| Unit | Node | How it is managed | Starts at boot |
|---|---|---|---|
| `aiops` (console, API, detector, workflows, exporter) on :5170 | Node-2 | systemd **user** unit `~/.config/systemd/user/aiops.service`, `Restart=on-failure` | yes (enabled, linger on) |
| `aiops-gateway` (model gateway) on :8790 | Node-2 | systemd user unit `~/.config/systemd/user/aiops-gateway.service`, `Restart=always` | yes (enabled, linger on) |
| `aiops-prometheus`, `aiops-grafana` | Node-3 | Docker Compose project `aiops-monitoring` in `~/aiops-monitoring`, `restart: unless-stopped` | yes (Docker enabled) |
| `aiops-backup.timer` / `aiops-backup.service` | Node-2 | systemd user timer, daily 02:30 (`Persistent=true`, up to 5 min random delay); oneshot `python -m backend.core.backup` | yes (enabled) |
| `aiops-test-svc` (+ `aiops-test-svc-2`, `-3` when scaled) | Node-2 | images built by `ops/deploy/test-service/build.sh`, containers managed by `backend/incidents/testsvc.py`, `--restart no` | **no, by design**: it is the disposable test target |

`aiops` and `aiops-gateway` read secrets from `~/sovereign-ai-ops/.env` (mode 600) via
`EnvironmentFile=`. The backup service needs none: it uses the SSH key to copy to Node-3.

### 2. First-time setup (Node-2)

```bash
cd ~/sovereign-ai-ops
uv sync                                   # creates .venv from pyproject.toml + uv.lock
(cd frontend && npm ci && npm run build)  # builds frontend/dist, served by the API process
# .env (mode 600): AIOPS_APPROVER_TOKEN=..., AIOPS_GATEWAY_KEY=...
uv run ops/scripts/users.py add admin admin            # prompts for a password
cp ops/deploy/systemd/*.service ops/deploy/systemd/*.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now aiops aiops-gateway aiops-backup.timer
sudo loginctl enable-linger tmc           # user units start at boot without a login
chmod 600 data/*.db* data/*.json data/session.key
ops/deploy/test-service/build.sh              # builds aiops-test-svc:v1 and :v2
.venv/bin/python -c "from aiops import testsvc; print(testsvc.deploy('v1', 'operator', 'initial deployment'))"
```

The unit files are kept in `ops/deploy/systemd/` in the repository; the copies in
`~/.config/systemd/user/` were identical on 2026-11-15. Their content is also reproduced in §7.

`ops/scripts/test-service.sh up` builds both images if they are missing and deploys v1 through
`backend.incidents.testsvc`, the same path the executor uses. `status`, `break` and `down` are also available.

### 3. Routine upgrade

```bash
cd ~/sovereign-ai-ops
.venv/bin/python -m pytest tests -q            # must pass before restarting
(cd frontend && npm run build)                 # only if the frontend changed; no restart needed
systemctl --user restart aiops                 # backend change
systemctl --user restart aiops-gateway         # gateway change (in-flight streams are cut)
systemctl --user status aiops aiops-gateway --no-pager
```

A frontend-only change needs no restart, because the API process serves `frontend/dist`
directly and `index.html` is always revalidated. Users should hard-refresh.

Restarting `aiops` interrupts running workflows. Their state is checkpointed in
`data/checkpoints.db`, and incidents waiting for approval resume when the approval is decided.

### 4. Monitoring stack (Node-3)

```bash
cd ~/sovereign-ai-ops/ops/deploy/monitoring
./deploy.sh            # regenerates dashboards, rsyncs to tmc@spark-3:~/aiops-monitoring, compose up -d, reloads Prometheus
```

Prometheus is reloaded with `docker kill -s HUP aiops-prometheus`. The HTTP lifecycle API
(`--web.enable-lifecycle`, `/-/reload`) is deliberately not enabled, so nobody on the LAN can
reload or shut down Prometheus over HTTP.

`deploy.sh` is idempotent. To change dashboards, edit `build_dashboards.py` and re-run it; the
dashboards are provisioned read-only in Grafana. Images are pinned to `prom/prometheus:v3.5.0`
and `grafana/grafana:12.1.1`.

Health checks:

```bash
curl -s http://<node-3-ip>:9090/-/ready
curl -s http://<node-3-ip>:9090/api/v1/targets | python3 -m json.tool | grep -c '"health": "up"'
curl -s http://<node-3-ip>:3000/api/health
```

### 5. Boot and power-loss behaviour

Verified on 2026-11-15:

| Item | State |
|---|---|
| `loginctl show-user tmc -p Linger` (Node-2) | `Linger=yes` |
| `aiops`, `aiops-gateway` | enabled |
| Docker and Spark Monitor on every node | enabled |
| Model servers Node-1 | systemd units enabled: `omnivoice`, `paddleocr-vl`, `qwen3-asr`, `qwen3-tts`, `whisper-turbo`, `whisper-urdu` |
| Model servers Node-2 | systemd units enabled: `vllm-qwen36`, `qwen3-embedding`, `qwen3-reranker` |
| Model servers Node-3 | Docker containers `qwen3-30b`, `qwen-embed`, `qwen-rerank` with restart policy `unless-stopped` |
| Model servers Node-4 | systemd units enabled: `vllm-asr`, `vllm-tts`, `vllm-whisper` |
| Monitoring stack Node-3 | `restart: unless-stopped` |

After a full power loss everything listed above comes back without intervention. Expected
sequence and what the console shows:

1. The platform starts before the models finish loading (vLLM loads take several minutes).
   Diagnoses that run in this window may end as "Needs a person", because no LLM endpoint
   answers yet. The workflow degrades and does not guess.
2. The detector holds back "model disappeared" alerts for 15 minutes after each node boots
   (`REBOOT_GRACE_S`).
3. A fresh platform process has no previous uptime for the nodes, so it does not raise
   "restarted" incidents for the outage it was part of.
4. Nodes that come up later than Node-2 may raise "unreachable" incidents. These close as
   **Recovered** once the node answers.

A power cut is a cold boot, which is also what clears the GB10 Xid 119 GPU hang. A warm
`reboot` does not; see the operations runbook.

### 6. Data and backups

| Data | Path | Notes |
|---|---|---|
| Incidents, audit, evidence, approvals | `data/aiops.db` | SQLite WAL |
| Workflow checkpoints | `data/checkpoints.db` | LangGraph |
| Users, session key, gateway keys | `data/users.json`, `data/session.key`, `data/gateway_keys.json` | mode 600 |
| Evaluation results | `data/evaluations.jsonl` (one line per run) | |
| Lab state | `data/soc_lab.json` | |
| Runbook vector cache | `data/knowledge_index.json` | rebuilt automatically from `knowledge/runbooks/` if lost |
| Test service deployment history | `data/testsvc_deployments.json` | rollback target depends on it |
| Backups | `~/aiops-backups/aiops-*.tar.gz` (Node-2, outside the project) and `~/aiops-backups/` (Node-3) | 14 kept on each; archive mode 600, directory 700 |
| Prometheus TSDB | Docker volume `aiops-monitoring_prometheus-data` (Node-3) | 30 d / 50 GB |
| Grafana state | Docker volume `aiops-monitoring_grafana-data` (Node-3) | dashboards are re-provisioned from files |

The SQLite databases, WAL and SHM files are mode 600.

Backups are automated by `aiops-backup.timer` (daily 02:30, `backend/core/backup.py`). Each run:
1. copies every `*.db` with the SQLite online backup API and the other files in `data/` as they
   are (excluding `backups/` and `api.log`), with a manifest of row counts;
2. restore-tests the archive in a scratch directory: `integrity_check` on every database, the
   audit hash chain, row counts against the manifest, `users.json` and `gateway_keys.json`;
3. copies it to `tmc@spark-3:~/aiops-backups/`;
4. keeps the newest 14 on each side;
5. records the result as the `backup-restore` evaluation (last run 2026-11-15 01:07: 7/7, 146
   audit events).

Run or check one by hand, and restore, as described in the operations runbook §8. The archives
are not encrypted and contain the user database, session key and gateway key digests.

### 7. Unit files (for re-creation)

`~/.config/systemd/user/aiops.service`:

```ini
[Unit]
Description=Sovereign AI Ops - SRE platform API + web console (:5170)
After=network-online.target

[Service]
WorkingDirectory=/home/tmc/sovereign-ai-ops
EnvironmentFile=/home/tmc/sovereign-ai-ops/.env
ExecStart=/home/tmc/sovereign-ai-ops/.venv/bin/uvicorn backend.web:app --host 0.0.0.0 --port 5170 --loop uvloop --http httptools --proxy-headers --forwarded-allow-ips=127.0.0.1
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

`~/.config/systemd/user/aiops-gateway.service`:

```ini
[Unit]
Description=Sovereign AI Ops - OpenAI-compatible model gateway (:8790)
After=network-online.target

[Service]
WorkingDirectory=/home/tmc/sovereign-ai-ops
EnvironmentFile=/home/tmc/sovereign-ai-ops/.env
ExecStart=/home/tmc/sovereign-ai-ops/.venv/bin/uvicorn backend.llmops.gateway:app --host 0.0.0.0 --port 8790 --loop uvloop --http httptools --no-access-log --timeout-keep-alive 75
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
```

`ops/deploy/systemd/aiops-backup.service` and `aiops-backup.timer`:

```ini
[Unit]
Description=Sovereign AI Ops backup with restore test and off-node copy
After=aiops.service

[Service]
Type=oneshot
WorkingDirectory=/home/tmc/sovereign-ai-ops
ExecStart=/home/tmc/sovereign-ai-ops/.venv/bin/python -m backend.core.backup
Nice=10
```

```ini
[Unit]
Description=Daily Sovereign AI Ops backup

[Timer]
OnCalendar=*-*-* 02:30:00
Persistent=true
RandomizedDelaySec=300

[Install]
WantedBy=timers.target
```

### 8. Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `AIOPS_DB` | `data/aiops.db` | Store location |
| `AIOPS_SCAN_SECONDS` | 20 | Detector interval |
| `AIOPS_DETECTOR` | 1 | Set to 0 to disable the detector loop (tests, replays) |
| `AIOPS_ALLOWED_NETS` | `127.0.0.0/8,::1/128,<office-lan>,100.64.0.0/10` | Client network allowlist |
| `AIOPS_APPROVER_TOKEN` | empty | CLI approver token |
| `AIOPS_GATEWAY_PORT` / `AIOPS_GATEWAY_URL` / `AIOPS_GATEWAY_KEY` | 8790 / local / empty | Gateway |
| `AIOPS_GATEWAY_ALIASES` | `{}` | Alias overrides as JSON |
| `AIOPS_GATEWAY_STATIC_UPSTREAMS` | `[]` | Upstreams not discovered via Spark Monitor |
| `AIOPS_GATEWAY_PREFER` | empty | Preferred node per model, `model=Node-N,...`: that node's replica takes every request, the others are standby |
| `AIOPS_API_RATE` / `AIOPS_API_BURST` | 20 / 200 | Console API rate limit per client IP (requests/s, burst) |
| `AIOPS_GATEWAY_RATE` / `AIOPS_GATEWAY_BURST` | 100 / 500 | Gateway rate limit per API key (requests/s, burst) |
| `AIOPS_PROMETHEUS_URL` | `http://<node-3-ip>:9090` | Prometheus for 7- and 30-day node history |
| `AIOPS_GRAFANA_URL` | `http://<node-3-ip>:3000` | Target of the "Metric history" sidebar link |
| `AIOPS_SSH_USER` | `tmc` | Account for read-only collection over SSH |

## Day-to-day operation

Day-2 operation of the platform. The console is at `http://<node-2-ip>:5170`
(Tailscale: `http://<node-2-tailscale-ip>:5170`).

### 1. Daily routine

1. **Overview page.** Act on anything in the approval banner, then check the server cards and the
   model-serving table. The quality figures (fixes accepted, conclusions backed by evidence, false
   alarms) come from recorded decisions; the false-alarm rate is only as good as the
   verdicts people give when closing incidents (§2).
2. **Approvals.** Every pending action shows what the AI proposes, the evidence it cites, the
   policy decision and the rollback. Approve or reject with a comment; the comment is kept in the
   audit trail.
3. **Incidents → Open.** Incidents in "Needs a person" or "Needs a person to follow up" need you
   to act; follow the AI's manual steps, then close the incident with a note.
4. **Grafana** (`http://<node-3-ip>:3000`, Fleet / Model serving / AI operations, or the
   sidebar link "Metric history") for history. Per-node 7-day and 30-day views are also on the
   node page in the console.
5. **Backups and audit integrity.** The Overview's "safety tests passing" count includes the
   nightly `backup-restore` evaluation (7 checks); the Audit log page shows whether the audit hash
   chain verifies (§7).

### 2. Incident lifecycle

| Status | Meaning | Who acts |
|---|---|---|
| Investigating | Workflow collecting evidence or diagnosing | nobody |
| Waiting for your approval | A write action was proposed and the policy requires approval | approver |
| Fixing / Checking the fix | Executor running, then independent verification | nobody |
| Fixed and verified (`resolved`) | Action ran and recovery was confirmed by two consecutive good checks | nobody |
| Recovered (`recovered`) | The detector saw the condition clear without an automated action; the outage duration is recorded | nobody (review if it recurs) |
| Needs a person (`needs_human`) | The workflow could not finish, for example because no LLM endpoint answered | operator |
| Needs a person to follow up (`diagnosed`) | Diagnosis complete; no automated action is allowed for this resource | operator |
| Fix did not work (`not_resolved`) | Action ran but verification failed | operator |
| Blocked by policy (`action_denied`) | The proposed action violated policy | operator |
| You rejected the action (`action_rejected`) | An approver rejected it | done |
| Closed by a person (`closed`) | Closed manually with a note | done |

Final states: `resolved`, `recovered`, `closed`, `action_rejected`. A recurrence of the same
problem after a final state opens a new incident.

**Closing:** approvers can close any incident that is not waiting for approval or executing. A
note is required and recorded. Also pick a verdict: "real problem" (`true_positive`), "false
alarm" (`false_positive`) or "expected activity" (`expected_activity`). The verdict is stored in
the `closed` audit event and feeds the false-positive rate on the Overview page.

**Self-recovery** (`backend/incidents/runtime.py`, `RECOVERABLE`) applies to:
- threshold kinds, re-evaluated from fresh data every scan (`METRIC_KINDS`): `high_memory`,
  `disk_usage`, `gpu_temperature`, `high_cpu`, `gpu_stalled`, `model_latency_high`,
  `model_queue_stuck`, `service_unhealthy`;
- `node_unreachable`;
- `model_server_missing`, `model_load_stalled` (the model must be serving again);
- `svc_container_stopped`, `svc_container_unhealthy`, `svc_container_restarting`;
- `svc_unit_failed`, `svc_unit_restarting`.

The condition must be positively observed as cleared, on a node that answered in the same scan;
the mere absence of the alert is not enough. Incidents that are executing, being verified or have
a running workflow are left alone.

**Expired approvals:** if an incident is waiting for approval and the problem clears first, the
pending approval is set to `expired` (audit event `approval_expired`, actor `detector`) and the
incident closes as Recovered. An approver who clicks at the same moment either wins or gets a
"no longer pending" answer; an action is never run for a problem that has gone.

### 3. What to do for each incident kind

Thresholds are in `backend/core/config.py`.

#### Infrastructure (`backend/incidents/detector.py`)

| Kind | Raised when | Operator action |
|---|---|---|
| `node_unreachable` | Spark Monitor on a node failed 3 consecutive polls (about 60 s) | Check power and network; ping the LAN and Tailscale addresses; `ssh tmc@spark-N`. Closes as Recovered when the node answers |
| `node_rebooted` | The node's uptime went backwards since the last good read | Confirm whether the reboot was planned. If not, read the AI's evidence (kernel Xid events, model servers, services) and check `journalctl -b -1` on the node |
| `high_memory` | Memory ≥ 92% (unified memory: model weights count) | Identify the largest model server (`get_model_servers` evidence). Reduce `gpu_memory_utilization` or move a model; never kill another team's server without its owner |
| `high_cpu` | CPU ≥ 90% for 9 consecutive scans (about 3 minutes) | Find the process with `top` on the node; it is rarely a model server |
| `gpu_stalled` | GPU utilisation 0% while a model server has requests running, for 9 consecutive scans | The signature of a hung engine or GPU. Check for Xid events (§4); otherwise the server's logs. Restarting the server is the node owner's decision |
| `model_latency_high` | A ready chat or vision model (LLM/VLM) serving traffic has first-token p95 above 10 s | Check queue depth and KV-cache use in the evidence; usually load. Lower concurrency at the client or add a replica |
| `disk_usage` | Root filesystem ≥ 85% | Clean model caches or old images with the owner's agreement |
| `gpu_unavailable` | Spark Monitor reports no GPU | Check `nvidia-smi` on the node. If it shows ERR!, treat as Xid 119 (§4) |
| `gpu_temperature` | GPU ≥ 85 °C | Check airflow and load |
| `gpu_xid` | NVRM Xid event in the kernel journal of the current boot | 119/154/79/48/95: hardware or firmware fault, follow §4. 13/31/43: usually the named application; restart that application |
| `model_load_stalled` | A vLLM server has been loading for over 15 minutes | Usually a GPU hang (check for Xid 119), otherwise the server's own logs |
| `model_queue_stuck` | Requests waiting and none running | Check the server's logs; restart the model service if it is wedged (node owner) |
| `model_server_missing` | A model that was serving has disappeared (held back for 15 min after a boot) | Restart the model's systemd unit or container on that node. Closes as Recovered when it serves again |
| `service_unhealthy` | The designated test service fails its health check | The AI reads the health body and the deployment history and proposes one of `rollback_test_deployment` (500 after a new release), `clear_test_cache` (503 cache full), `scale_test_service` with `replicas` 2 or 3 (503 saturated) or `restart_test_service` (stopped). Check that the proposal matches the health body, then approve; the result is verified automatically |

#### Services (`backend/monitoring/services.py`)

| Kind | Raised when | Operator action |
|---|---|---|
| `svc_container_unhealthy` | A container's health check fails | Read its logs; restart it if the owner agrees |
| `svc_container_restarting` | A container is in a restart loop | Read its logs for the crash cause |
| `svc_container_stopped` | A container seen running has stopped (one alert per transition) | Start it again if the stop was not intended. Model containers are reported as `model_server_missing` instead |
| `svc_unit_failed` / `svc_unit_restarting` | A systemd unit failed or keeps restarting | `systemctl status <unit>` and `journalctl -u <unit>` on the node |

`iwpmd.service` fails at every boot by design (DGX Spark has no iWARP NIC). It is shown as
"Known" and never raises an incident.

#### Security (`backend/security/rules.py`)

| Kind | Raised when | Operator action |
|---|---|---|
| `sec_bruteforce` | Repeated failed logins from one source on one node | Read the AI's assessment. On the real fleet containment is advisory only; block the source at the network edge if malicious |
| `sec_password_spray` | One source tries many accounts | As above |
| `sec_login_after_failures` | A successful login after failures from the same source on the same node | Treat as a possible compromise until confirmed: verify with the account owner, review `sudo` activity |
| `sec_sudo_failures` | Repeated sudo authentication failures by one user | Confirm with the user |
| `sec_malicious_file` | A file whose SHA-256 is in the local IOC list was written | Lab: approve `quarantine_lab_file` for that path. A real node would need manual incident response |
| `sec_beaconing` | One host connects to one address and port at regular intervals (at least 5 in 30 min, intervals within 20%) | Lab: `isolate_lab_endpoint` or `block_ip_lab` for the remote address. On a real node, identify the process before acting |
| `sec_vulnerable_package` | The 6-hourly scan found a catalogued CVE in an installed package with no fix in its changelog | Upgrade the package (`sudo apt install --only-upgrade <package>`) with the node owner. The incident is a statement of exposure, not of an attack |

Containment actions (`block_ip_lab`, `disable_lab_account`, `isolate_lab_endpoint`,
`quarantine_lab_file`) act on the isolated lab only and are bound by policy to the incident's own
subject.

### 4. GB10 GPU hang (Xid 119) procedure

Symptoms:
- a vLLM load stalls after a few weight shards (for example 2 of 42);
- `nvidia-smi` shows `ERR!`;
- the kernel journal shows `NVRM: Xid 119` and floods of `bad register read ... 0xbadf5600`.

1. Confirm: `journalctl -k -b 0 | grep -E 'NVRM: Xid'` on the node.
2. Do **not** rely on a service restart or `sudo reboot`. On this fleet neither has cleared Xid
   119.
3. Cold power-off:
   1. `sudo shutdown -h now`;
   2. remove power for about 30 seconds;
   3. power on.
4. After boot, the models come back automatically (see the deployment runbook). The detector
   holds "model disappeared" alerts for 15 minutes while they load.
5. Known triggers on this fleet are the desktop session (Xorg, gnome-control-center) and vLLM
   model loads.

### 5. Monitoring access

| Tool | URL | Access |
|---|---|---|
| Grafana | `http://<node-3-ip>:3000` | User `admin`; the password is in `~/aiops-monitoring/.env` on Node-3 (and `ops/deploy/monitoring/.env` on Node-2), mode 600 |
| Prometheus | `http://<node-3-ip>:9090` | No login (LAN). Alert rules under Alerts; targets under Status → Targets |
| Exporter | `http://<node-2-ip>:5170/metrics` | Fleet node IPs and localhost only |

Prometheus alert rules (`ops/deploy/monitoring/prometheus/rules.yml`, 8 rules) watch the platform
itself:
- `AIOpsPlatformDown`, `AIOpsDetectorStale` and `AIOpsDetectorErrors`: the detector is not
  opening incidents;
- `AuditChainBroken`: the audit hash chain failed verification (§7);
- `GatewayDown`;
- `NodeMonitorDown`, `NodeMemoryHigh`, `ModelQueueBacklog`.

There is no Alertmanager. Check the Prometheus Alerts page or the Grafana "AI operations"
dashboard.

### 6. Useful commands (Node-2)

```bash
systemctl --user status aiops aiops-gateway --no-pager
journalctl --user -u aiops -n 100 --no-pager
ops/scripts/test-service.sh break      # stop the test service (demo scenario)
.venv/bin/python -c "from aiops import testsvc; print(testsvc.status())"   # release, rollback target, replicas
.venv/bin/python ops/evaluation/scenarios/sre_test_service.py                  # four-fault drill; leaves v1 healthy
uv run ops/scripts/users.py list
uv run ops/scripts/users.py add NAME approver --generate
sqlite3 data/aiops.db "select id,status,kind,node,title from incidents order by created desc limit 10"
```

`ops/scripts/test-service.sh up` builds the images if missing and deploys release v1 through
`backend.incidents.testsvc` (recorded in the deployment history); `status` shows release, rollback target and replicas.

### 7. Audit integrity

The runtime re-verifies the audit hash chain every 10 minutes. Check it on demand:

```bash
set -a; . ./.env; set +a            # loads AIOPS_APPROVER_TOKEN into this shell
curl -s -H "X-Approver-Token: $AIOPS_APPROVER_TOKEN" 'http://127.0.0.1:5170/api/audit/integrity?fresh=true'
.venv/bin/python -c "from aiops import store; print(store.verify_audit_chain())"
```

If `AuditChainBroken` fires or the result shows `ok: false`:
1. Do not restart or "repair" anything; the database is evidence. Note `first_bad_seq` and
   `reason` (gap or reordering, or hash mismatch).
2. Copy `data/aiops.db*` aside (`sqlite3 data/aiops.db ".backup '/tmp/aiops-evidence.db'"`).
3. Verify the latest backups (§8). The last archive whose chain verifies bounds when the change
   happened.
4. Compare the rows from `first_bad_seq` onwards with the same rows in that backup to see what
   was altered. Treat it as a security incident: only an account with write access to
   `data/aiops.db` on Node-2 could have done it.

### 8. Backups and restore

`aiops-backup.timer` runs daily at 02:30. Each run backs up, restore-tests, copies to Node-3 and
records the `backup-restore` evaluation (7 checks).

```bash
systemctl --user list-timers aiops-backup.timer
journalctl --user -u aiops-backup -n 20 --no-pager
ls -l ~/aiops-backups/ ; ssh tmc@spark-3 'ls -l ~/aiops-backups/'
.venv/bin/python -m backend.core.backup                              # run one now
.venv/bin/python -m backend.core.backup --verify ~/aiops-backups/aiops-YYYYMMDD-HHMMSS.tar.gz
```

Restore (Node-2, after verifying the archive):
1. `systemctl --user stop aiops aiops-gateway`.
2. Move the current `data/` aside (keep it).
3. `tar -xzf <archive> -C /tmp` and copy the files from `/tmp/aiops-<stamp>/` into `data/`
   (not `MANIFEST.json`); `chmod 600 data/*.db data/*.json data/session.key`.
4. `systemctl --user start aiops aiops-gateway`, then check `/api/audit/integrity?fresh=true`.

Incidents and audit events after the backup time are lost. Archives contain the user database,
the session key and gateway key digests; handle them as secrets.

### 9. Runbook knowledge

The agent's runbooks are the Markdown files in `knowledge/runbooks/`. One `## ` section is one
retrievable unit, so keep each section self-contained. Put `always: true` front matter only on
short platform facts that every incident needs. Edits take effect on the next incident without a
restart: changed sections are re-embedded and get a new version. Each incident's
`retrieve_runbooks` step and report show which sections and versions the agent saw, and whether
retrieval used `dense+rerank`, `dense` or the `keyword` fallback (embedding model unreachable).

### 10. Known gaps

| Gap | Effect | Fix (needs the node owner or sudo) |
|---|---|---|
| Account `tmc` is not in the `docker` group on Node-4 | Node-4 containers are not visible to service monitoring | `sudo usermod -aG docker tmc` on Node-4 |
| Node-1 port 8012 is closed by another project | PaddleOCR is not scraped directly or routed by the gateway; still reported via Spark Monitor | None; this is intended by that project |
| Platform runs only on Node-2 | If Node-2 is down, no incidents are opened; Prometheus on Node-3 still records the outage (`AIOpsPlatformDown`) | Accepted for now |
| Weak passwords on two console accounts | Account compromise on the LAN | Change them in Account → Change password |

### Decommissioned containers (2026-11-16)

On the owner's request, 19 unused containers were stopped:

| Node | Containers |
|---|---|
| Node-1 | agpb (3), agpension-hesk (3), livekit-voice-agent (4) |
| Node-2 | agpb (5), qdrant, saad-claims-redis |
| Node-3 | satori-caddy, satori-pg17 |

Their restart policy was set to `no` so they stay stopped after a reboot.

- **What changed.** Nothing was removed: containers, images and volumes are intact.
- **Previous restart policies:** `data/decommissioned-restart-policies.json`.
- **Register.** They are listed in `data/decommissioned.json`, which `backend/monitoring/services.py` reads so that a planned
  stop does not open an incident. Each entry is audited as `container_decommissioned`.

To bring one back (example `agpb-postgres-1` on Node-1, previous policy `unless-stopped`):

```bash
ssh tmc@<node-1-ip> 'docker update --restart=unless-stopped agpb-postgres-1 && docker start agpb-postgres-1'
# then remove "Node-1/agpb-postgres-1" from data/decommissioned.json, so a future unplanned stop alerts again
```
