# Sovereign AI Ops

A self-hosted operations platform for an on-premises GPU cluster: **AIOps**, **LLMOps** and **AgentOps** in a
single console. Built for a fleet of four NVIDIA DGX Spark nodes, it runs entirely on that fleet against
self-hosted models. No data leaves the cluster.

Incidents are detected by deterministic rules, investigated by an LLM agent, fixed only after a human
approves, independently verified, and recorded in a tamper-evident audit trail.

## Features

**AIOps: incident response**
- Deterministic detector (no LLM in the loop) for GPU stalls and driver faults (NVRM Xid), stuck model queues,
  latency, missing model servers, service failures, reboots and resource pressure.
- LangGraph investigation agent: collects evidence, retrieves runbooks (RAG), diagnoses and proposes a fix.
- Deterministic policy engine and restricted executor: write actions need human approval and are limited to an
  allowlist of resources, operations and parameters.
- Independent verification of every recovery, and a SHA-256 hash-chained, append-only audit log.
- Fleet, service and GPU monitoring, with history from Prometheus and log search in Loki.
- SOC module: SSH and sudo telemetry, detection rules mapped to MITRE ATT&CK, offline CVE relevance, and an
  isolated lab for containment drills.

**LLMOps: model serving**
- OpenAI-compatible gateway with service discovery, health probes, circuit breaking, cross-node failover,
  API keys and per-key rate limits.
- Request log (metadata only, never prompt or response text) and GPU energy per model.
- Scheduled quality evaluations of every model with exact checks (no judge model), and regression alerts.
- Model registry: engine version, weights digest and context length per server, with scores before and after
  each change.

**AgentOps: agent oversight**
- Every investigation replayed step by step, with each model call's latency, tokens, retries and fallbacks.
- Approver verdicts on each diagnosis, giving a measured diagnosis accuracy.

## Architecture

```
Node telemetry agents ──┐
Container events ───────┼──> Detector (rules) ──> Incident
Service health checks ──┘                            │
                                                     ▼
          LangGraph agent: collect evidence -> retrieve runbooks -> diagnose -> plan
                                                     │
                                         Policy engine (deterministic)
                              ┌──────────────────────┴──────────────────────┐
                        allow / deny                         requires approval -> human, in the console
                              │                                             │ approved
                              ▼                                             ▼
                           Report  <────────── verify  <────────── restricted executor
```

| Component | Technology |
|---|---|
| Backend | Python 3.12, FastAPI, LangGraph, SQLite (WAL) |
| Frontend | React 19, TypeScript, Vite, TanStack Query, ECharts |
| Model gateway | FastAPI on uvicorn (uvloop, httptools), OpenAI-compatible API |
| Model serving | vLLM, SGLang, Ollama, vLLM-Omni (discovered and monitored, not managed) |
| Observability | Prometheus, Grafana, Loki, Grafana Alloy |
| Deployment | systemd user services, Docker |

## Repository layout

```
backend/
  core/          configuration, SQLite store and audit chain, backups, LLM client, rate limits
  incidents/     detector, LangGraph agent, policy engine, executor, verification, runbook retrieval
  monitoring/    fleet snapshot, model engines, services, GPU driver events, Prometheus, Loki
  llmops/        model gateway, request log, model evaluations, model registry
  agentops/      agent run history and diagnosis verdicts
  security/      SOC rules and telemetry, ATT&CK mapping, CVE relevance, users and roles
  web/           FastAPI application, security middleware, API routes
frontend/        web console (React)
tests/           backend test suite (pytest)
knowledge/       runbooks used by the agent, CVE catalogue
ops/
  deploy/        systemd units, monitoring stack, log-shipping agent, test service
  scripts/       CLI, user management, utilities
  bench/         inference and failover benchmarks
  evaluation/    end-to-end evaluation scenarios
docs/            architecture, operations and evaluation
```

## Getting started

**Requirements:** Python 3.12, [uv](https://docs.astral.sh/uv/), Node.js 20 or later, and Docker on the
monitored nodes.

```bash
# backend dependencies
uv sync

# web console
cd frontend && npm ci && npm run build && cd ..

# first administrator
uv run ops/scripts/users.py add admin admin

# console and API on :5170, model gateway on :8790
uv run uvicorn backend.web:app --host 0.0.0.0 --port 5170
uv run uvicorn backend.llmops.gateway:app --host 0.0.0.0 --port 8790
```

**Configuration.** The fleet (nodes, model endpoints and the resources the platform may act on) is declared in
`backend/core/config.py`. Node addresses, credentials and tokens are not stored in the repository: they are
read from a `.env` file at the repository root (mode 600), for example `AIOPS_NODE_1="<lan-ip>,<tailscale-ip>"`.
See [docs/operations.md](docs/operations.md) for every variable, the systemd units in `ops/deploy/systemd/`,
and deployment of the monitoring stack.

## Testing

```bash
uv run pytest tests -q                     # backend suite
cd frontend && npm run test:smoke          # opens every page of the console in a headless browser
```

Tests that check live Prometheus, Loki and the gateway are skipped when those are unreachable. End-to-end
scenarios (incident drills, gateway failover, prompt injection, SOC lab) are in `ops/evaluation/scenarios/`,
with results in [docs/evaluation.md](docs/evaluation.md).

## Security model

- The console accepts only allowlisted networks, then requires a login (scrypt-hashed passwords) with roles
  `viewer` < `approver` < `admin`, CSRF protection and a strict Content Security Policy.
- The LLM never chooses a target. Every resource it may act on is declared server-side, and the executor
  re-checks approval and policy before running a fixed command.
- Autonomous remediation is limited to a dedicated test service and the SOC lab. Production model servers
  are investigate-and-recommend only.
- Tool output and logs are treated as untrusted input; prompt-injection resistance is covered by tests and a
  live evaluation.

## Documentation

- [Architecture](docs/architecture.md): design, network and security, design decisions, risk register
- [Operations](docs/operations.md): platform reference, deployment, day-to-day operation, incident procedures
- [Evaluation](docs/evaluation.md): evaluation plan, MTTR baseline, inference and gateway benchmarks
