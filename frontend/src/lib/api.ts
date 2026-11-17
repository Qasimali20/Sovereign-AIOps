export type Role = 'viewer' | 'approver' | 'admin'
export type User = { username: string; role: Role }
export const canApprove = (u: User) => u.role === 'approver' || u.role === 'admin'

export type IncidentRow = {
  id: string; created: number; updated: number; status: string; kind: string
  node: string; resource: string; title: string; pending_approval: boolean; running: boolean
  origin?: 'real' | 'drill' | 'planned' | 'merged'; occurrences?: number
}
export type Evidence = { id: string; created: number; source: string; tool: string; data: unknown }
export type Step = { id: string; created: number; node: string; data: any }
export type AuditEvent = { id: string; created: number; incident_id: string | null; actor: string; event: string; data: any }
/* log search (Loki): lines newest first, bounded by the backend */
export type LogEntry = { ts: number; time: string; service: string; severity: string; component: string; line: string }
export type LogResult = {
  query: string; window: { start: number; end: number }; limit: number; returned: number; truncated: boolean
  lines_by_service: Record<string, number>; entries: LogEntry[]; node?: string
}
export type ActiveCounts = { active: number; in_progress: number; awaiting_approval: number; needs_person: number }
export type Approval = {
  id: string; action_id: string; incident_id: string; created: number; status: string
  approver: string | null; decided: number | null; comment: string | null
}
export type Proposal = {
  incident_id: string; action_id: string; operation: string
  target: { resource_type: string; resource_id: string }
  reason: string; evidence_refs: string[]; expected_outcome: string; rollback_plan: string
  model_meta?: { endpoint: string; latency_s: number }
}
export type Observation = { fact: string; evidence_ids: string[] }
export type Hypothesis = {
  cause: string; confidence: 'low' | 'medium' | 'high'
  supporting_evidence_ids: string[]; missing_information: string[]
}
export type Report = {
  final_status: string; diagnosis_summary: string | null
  observations: Observation[]; hypotheses: Hypothesis[]
  observation_evidence_support: number | null
  proposal: Proposal | null; recommended_manual_steps: string[]
  policy: { decision: string | null; reasons: string[] | null }
  approval: { id: string; status: string; approver: string; comment: string } | null
  execution: any; verification: string | null; errors: string[]; model: string | null
  assessment?: string | null
  attack_techniques?: { technique_id: string; name?: string; tactic?: string; confidence: 'low' | 'medium' | 'high'; rationale: string; evidence_ids: string[] }[]
}
export type Bundle = {
  incident: IncidentRow & { detail: any; fingerprint: string }
  evidence_records: Evidence[]; agent_steps: Step[]; audit_events: AuditEvent[]
  action_proposals: { id: string; created: number; data: Proposal }[]
  approval_requests: Approval[]
  policy_decisions: { id: string; action_id: string; created: number; decision: string; reasons: string }[]
  execution_records: { id: string; action_id: string; created: number; ok: number; data: any }[]
  verification_results: { id: string; created: number; outcome: string; data: any }[]
  report: Report | null; running: boolean
}
export type ModelServer = {
  name: string; kind: string; port: number; status: string; started: number | null; mem_fraction: number | null
  running: number | null; waiting: number | null; kv_cache: number | null; tokens_per_s: number | null
  engine?: 'vllm' | 'vllm-omni' | 'sglang' | 'ollama'; mem_gb?: number | null
  ttft_p95: number | null; e2e_p95: number | null; preemptions: number | null; req_per_min?: number | null
  full_name?: string     // complete model name from the loaded weights (e.g. Qwen3-Embedding-0.6B for qwen-embed)
}
export type FleetNode = {
  host: string; ip: string; ok: boolean; error?: string; via?: string
  uptime_h?: number; cpu_percent?: number; cpu_temp_c?: number; mem_used_gb?: number; mem_total_gb?: number
  disk_used_pct?: number; disk_used_gb?: number | null; disk_total_gb?: number | null
  net_rx_bps?: number | null; net_tx_bps?: number | null; gpu?: { name: string; util: number | null; temp: number | null; power: number | null; clock_sm: number | null } | null
  gpu_apps?: number; servers?: ModelServer[]
}
export type Fleet = Record<string, FleetNode>
export type BoardPanel = {
  id: string; title: string; unit: string; max: number | null; desc: string; section: string; by: string | null
  series: { key: string; name: string; slot: number | null }[]; points: (number | null)[][]; hidden: string[]; error: string | null
  kind: 'line' | 'stacked' | 'heatmap' | 'timeline' | 'bars' | 'donut'; warn: number | null; warn_label: string; absent?: string
  events: { series: number; t: number; kind: string }[]
}
export type ScoreRow = {
  model: string; node: string; kind: string; relayed: boolean
  rpm: number | null; requests: number | null; failed_pct: number | null; ttft_p95: number | null; tpot_p95: number | null
  ttft_p99: number | null; e2e_p99: number | null; ttfa_p95?: number | null; ttfa_p99?: number | null; rtf_p50?: number | null
  queue_p95: number | null; e2e_p95: number | null; prefix_hit_pct: number | null; preemptions: number | null; avg_prompt?: number | null
  kv_pct: number | null; running: number | null; waiting: number | null
}
export type ModelRef = { id: string; name: string; node: string; kind: string; engine?: string }
export type ModelBoard = {
  model: ModelRef; models: ModelRef[]; range: string; step: number; start: number; end: number
  summary: ScoreRow | null; panels: BoardPanel[]
  incidents: { id: string; kind: string; status: string; title: string; created: number }[]
}
export type Board = {
  board: string; title: string; range: string; step: number; start: number; end: number
  stats: { title: string; unit: string; desc: string; value: number | null }[]
  panels: BoardPanel[]; boards: Record<string, string>
}
/** history points: [t, gpu_util, cpu_pct, mem_pct]; model points: [t, tok/s, prompt tok/s, running, waiting, kv, ttft_p95, e2e_p95, req/min] */
export type NodeHistory = {
  node: string; range: string; source: string; points: (number | null)[][]
  models: Record<string, { name: string; kind: string; port: number; points: (number | null)[][] }>
}
export type SystemInfo = {
  grafana_url?: string
  grafana_tailnet_url?: string
  uptime_s: number
  detector: { last: number | null; last_alerts: number; scans: number; error: string | null; interval_s: number }
  workflows_running: string[]
  llm_endpoints: { name: string; model: string; base_url: string; healthy: boolean }[]
  event_recorder: { events_recorded: number }
  kernel_coverage: Record<string, { access: 'local' | 'ssh'; ok: boolean; error: string | null; checked: number }>
}
export type PolicyView = {
  autonomy: { level: number; scope: string }
  operations: { operation: string; kind: string; risk: string; decision: string }[]
  resources: { resource_id: string; resource_type: string; node: string; container: string; health_url: string; allowed_operations: string[] }[]
  readonly_tools: string[]
  detector: Record<string, number>
  rules: string[]
}
export type EvalRun = { t: number; passed: number; total: number; ok: boolean; meta: Record<string, unknown> }
export type Overview = {
  window_days: number
  real_only: boolean
  by_status: Record<string, number>
  by_category: Record<string, number>
  pipeline: {
    detected: number; diagnosed: number; proposed: number
    policy: { allow: number; requires_approval: number; deny: number }
    approval: { pending: number; approved: number; rejected: number }
    executed: number; verified: { resolved: number; not_resolved: number }
    closed_by_human: number; open: number; recovered?: number
  }
  timing: { detect_to_diagnosis_s: number | null; detect_to_proposal_s: number | null; approval_wait_s: number | null; approval_to_verified_s: number | null }
  quality?: { proposal_acceptance: number | null; evidence_support_mean: number | null; false_positive_rate: number | null; human_verdicts: number; tokens_per_incident_median: number | null; tokens_total: number }
  guardrails: { policy_denials: number; refused_tool_requests: number; executor_refusals: number; executions_total: number; executions_without_human_approval: number }
  daily: { t: number; opened: number; resolved: number }[]
  active: ActiveCounts
  recent: { id: string; created: number; status: string; kind: string; node: string; resource: string; title: string; operation: string | null; target: string | null; policy: string | null }[]
  evaluations: { name: string; description: string; run: EvalRun | null }[]
  sovereign: {
    gateway_up: boolean; requests: number; errors: number; prompt_tokens: number; completion_tokens: number
    replicas_healthy: number; replicas_total: number; uptime_s: number | null; model_endpoints: number
    endpoints_outside_private_network: string[]
    aliases: Record<string, { models: string[]; healthy_replicas: number; nodes: string[] }>
  }
  fleet: {
    nodes_total: number; nodes_reachable: number; models_total: number; models_ready: number
    kernel_coverage: number; kernel_total: number; gpu_xid_incidents_24h: number
    nodes: Record<string, { ok: boolean; mem_pct: number | null; disk_pct: number | null; gpu_temp: number | null; models: number; models_ready: number; kernel_ok: boolean | undefined; services: ServiceSummary }>
  }
}
export type SecEvent = { t: number; node: string; kind: string; user: string | null; src_ip: string | null; method: string | null; detail: string | null }
export type SecurityOverview = {
  hours: number; events_total: number; platform_checks?: number; events_by_kind: Record<string, number>; events_by_node: Record<string, number>
  top_sources: { src_ip: string; failures: number; successes: number; users: number; nodes: string[]; last: number; classification: string }[]
  recent: SecEvent[]
  incidents: { id: string; created: number; status: string; kind: string; node: string; resource: string; title: string; assessment: string | null }[]
  techniques: { id: string; name: string; tactic: string; incidents: number; high: number }[]
  coverage: Record<string, { ok: boolean; error: string | null; checked: number }>
  lab: { host: string; accounts: string[]; scenarios: Record<string, string>
         blocked_ips: Record<string, { by: string; at: number; incident: string }>
         isolated_endpoints?: Record<string, { by: string; at: number; incident: string }>
         quarantined_files?: Record<string, { by: string; at: number; incident: string }>
         disabled_accounts: Record<string, { by: string; at: number; incident: string }> }
}
export type ServiceSummary = { ok: boolean; docker_ok?: boolean; containers_running?: number; containers_unhealthy?: number; containers_restarting?: number; units_failed?: number; units_known_failed?: number; app_units?: number }
export type Container = { name: string; state: string; status: string; health: string | null; image: string }
export type NodeServices = { ok: boolean; error?: string | null; checked?: number; docker_ok?: boolean; containers: Container[]
  problem_units?: { unit: string; sub: string; known: string | null }[]; app_units?: string[]; summary: ServiceSummary }
export type AuditQuery = { limit?: number; actor?: string; event?: string; incidentsOnly?: boolean }

export type UserRow = { username: string; role: Role; created: number | null }

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) { super(message); this.status = status }
}

let onUnauthorized: () => void = () => undefined
export const setUnauthorizedHandler = (fn: () => void) => { onUnauthorized = fn }

export const DEMO = import.meta.env.VITE_DEMO === '1'      // offline snapshot build (npm run build:demo)

async function req<T>(path: string, body?: unknown): Promise<T> {
  if (import.meta.env.VITE_DEMO === '1') return (await import('../demo')).request<T>(path, body)   // literal: lets the bundler drop it
  const r = await fetch(`/api${path}`, {
    method: body === undefined ? 'GET' : 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'aiops' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  if (!r.ok) {
    let msg = r.statusText
    try {
      const j = await r.json()
      msg = typeof j.detail === 'string' ? j.detail : Array.isArray(j.detail) ? j.detail.map((d: any) => d.msg).join('; ') : msg
    } catch { /* not json */ }
    if (r.status === 401 && path !== '/login' && path !== '/me') onUnauthorized()
    throw new ApiError(r.status, msg)
  }
  return r.json()
}

const q = encodeURIComponent
// AgentOps: the incident agent's runs (read back from its recorded steps) and human verdicts on its diagnoses
export type Verdict = 'correct' | 'partly' | 'wrong'
export type RunSummary = { started: number; ended: number; duration_s: number; steps: number; model_calls: number; model_seconds: number
  attempts: number; fallbacks: number; prompt_tokens: number; completion_tokens: number; models: string[] }
export type AgentRun = RunSummary & { incident_id: string; title: string; kind: string; node: string; status: string; origin: string
  feedback: Verdict | null }
export type ModelCall = { endpoint: string | null; model: string | null; request_id: string | null; latency_s: number | null
  attempts: number | null; prompt_tokens: number; completion_tokens: number; skipped: string[] }
export type RunStep = { node: string; label: string; done_at_s: number; duration_s: number | null; detail: string; call: ModelCall | null }
export type RunDetail = { incident: { id: string; title: string; kind: string; node: string; status: string; origin: string }
  summary: RunSummary; steps: RunStep[]; feedback: { verdict: Verdict; note: string; by: string; created: number } | null }
export type AgentOverview = { days: number; runs: number; median_run_s: number | null; model_calls: number; tokens: number
  retries: number; fallbacks: number; rated: number; accuracy: number | null; verdicts: Record<Verdict, number> }

// LLMOps: what the gateway served (metadata only) and the GPU energy attributed to each model
export type LlmRange = '1h' | '6h' | '24h' | '7d'
export type LlmGroup = { requests: number; errors: number; error_rate: number; p50_s: number | null; p95_s: number | null
  ttft_p95_s: number | null; prompt_tokens: number; completion_tokens: number; audio_s: number; busy_s: number; endpoints: string[] }
export type LlmSummary = { range: LlmRange; since: number; until: number; step: number
  total: { requests: number; errors: number; error_rate: number | null; p95_s: number | null; tokens: number; clients: number }
  by_model: (LlmGroup & { model: string })[]; by_client: (LlmGroup & { client: string })[]
  outcomes: Record<string, number>; series: [number, number, number][] }
export type LlmRequest = { id: string; t: number; client: string | null; requested: string; model: string | null; node: string | null
  endpoint: string | null; status: number | null; outcome: string; latency_s: number | null; ttft_s: number | null
  prompt_tokens: number | null; completion_tokens: number | null; audio_s: number | null; attempts: number | null }
export type LlmEnergy = { range: LlmRange; hours: number; since: number; total_wh: number; active_wh: number; price_per_kwh: number | null; currency: string; method: string
  nodes: { node: string; avg_w: number; idle_w: number; total_wh: number; active_wh: number }[]
  models: { model: string; node: string; busy_s: number; requests: number; tokens: number; share: number; wh: number
    wh_per_request: number | null; wh_per_1k_tokens: number | null; cost: number | null }[] }

// LLMOps: quality evaluations per model, and the model registry (identity versions, track and compare only)
export type EvalCase = { case: string; ok: boolean; got?: string; score?: number }
export type EvalResult = { model: string; suite: string; score: number | null; id?: string; run_id?: string; t?: number; node?: string | null
  passed?: number; total?: number; latency_s?: number; error?: string | null; cases?: EvalCase[]; previous?: number | null
  baseline?: number | null; regressed?: boolean }
export type EvalsState = { running: boolean; last_run: number | null; interval_s: number; models: EvalResult[] }
export type ModelVersion = { id: string; node: string; port: number; model: string; kind: string | null; engine: string | null
  engine_version: string | null; root: string | null; max_model_len: number | null; digest: string | null; quantization: string | null
  first_seen: number; last_seen: number; started: number | null; restarts: number; versions?: number; first_known?: boolean
  evals_since?: { runs: number; mean: number | null }; evals_before?: { runs: number; mean: number | null } | null }
export type RegistryState = { refresh_s: number; models: ModelVersion[]; changes: AuditEvent[] }

export const api = {
  me: () => req<User>('/me'),
  login: (username: string, password: string) => req<User>('/login', { username, password }),
  logout: () => req<{ ok: boolean }>('/logout', {}),
  fleet: () => req<Fleet>('/fleet'),
  scorecard: (range: string) => req<ScoreRow[]>(`/metrics/scorecard?range=${q(range)}`),
  modelBoard: (model: string, range: string) => req<ModelBoard>(`/metrics/model?model=${q(model)}&range=${q(range)}`),
  metricsBoard: (board: string, range: string) => req<Board>(`/metrics/board/${q(board)}?range=${q(range)}`),
  nodeHistory: (node: string, range: string) => req<NodeHistory>(`/fleet/${q(node)}/history?range=${q(range)}`),
  services: () => req<Record<string, NodeServices>>('/services'),
  security: (hours = 24) => req<SecurityOverview>(`/security/overview?hours=${hours}`),
  runLab: (scenario: string) => req<{ ok: boolean; events: number; opened: { id: string; title: string }[] }>(`/security/lab/${q(scenario)}`, {}),
  resetLab: () => req<{ ok: boolean }>('/security/lab-reset', {}),
  overview: (days = 7, origin: 'real' | 'all' = 'real') => req<Overview>(`/overview?days=${days}&origin=${origin}`),
  system: () => req<SystemInfo>('/system'),
  policy: () => req<PolicyView>('/policy'),
  incidents: () => req<IncidentRow[]>('/incidents?limit=1000'),
  incident: (id: string) => req<Bundle>(`/incidents/${q(id)}`),
  llmSummary: (range: LlmRange) => req<LlmSummary>(`/llm/summary?range=${range}`),
  llmEnergy: (range: LlmRange) => req<LlmEnergy>(`/llm/energy?range=${range}`),
  llmRequests: (range: LlmRange, f: { model?: string; client?: string; outcome?: string }) =>
    req<LlmRequest[]>(`/llm/requests?range=${range}&model=${q(f.model ?? '')}&client=${q(f.client ?? '')}&outcome=${q(f.outcome ?? '')}&limit=200`),
  evals: () => req<EvalsState>('/llm/evals'),
  evalHistory: (model: string) => req<EvalResult[]>(`/llm/evals/history?model=${q(model)}`),
  runEvals: () => req<{ started: boolean }>('/llm/evals/run', {}),
  registry: () => req<RegistryState>('/llm/registry'),
  agentOverview: (days = 30) => req<AgentOverview>(`/agent/overview?days=${days}`),
  agentRuns: () => req<AgentRun[]>('/agent/runs?limit=500'),
  agentRun: (id: string) => req<RunDetail>(`/agent/runs/${q(id)}`),
  rateDiagnosis: (id: string, verdict: Verdict, note: string) =>
    req<{ verdict: Verdict }>(`/agent/runs/${q(id)}/feedback`, { verdict, note }),
  rerunIncident: (id: string) => req<{ ok: boolean }>(`/incidents/${q(id)}/rerun`, {}),
  closeIncident: (id: string, note: string, disposition?: string) => req<{ ok: boolean }>(`/incidents/${q(id)}/close`, { note, disposition: disposition || null }),
  approvals: (status = 'pending') => req<(Approval & { proposal: Proposal | null; incident: Pick<IncidentRow, 'id' | 'title' | 'node' | 'kind' | 'status' | 'origin'> | null })[]>(`/approvals?status=${q(status)}`),
  incidentSummary: () => req<ActiveCounts>('/incidents/summary'),
  decide: (approvalId: string, approve: boolean, comment: string) =>
    req<{ ok: boolean; status: string }>(`/approvals/${q(approvalId)}`, { approve, comment }),
  scan: () => req<{ opened: { id: string; title: string }[] }>('/scan', {}),
  auditIntegrity: () => req<{ ok: boolean | null; events?: number; head?: string; first_bad_seq?: number; reason?: string; checked?: number }>('/audit/integrity'),
  audit: (p: AuditQuery = {}) =>
    req<AuditEvent[]>(`/audit?limit=${p.limit ?? 500}&actor=${q(p.actor ?? '')}&event=${q(p.event ?? '')}&incidents_only=${!!p.incidentsOnly}`),
  users: () => req<UserRow[]>('/users'),
  saveUser: (username: string, password: string, role: Role) => req<{ ok: boolean }>('/users', { username, password, role }),
  deleteUser: (username: string) => req<{ ok: boolean }>(`/users/${q(username)}/delete`, {}),
  changePassword: (current: string, next: string) => req<{ ok: boolean }>('/me/password', { current, new: next }),
}
