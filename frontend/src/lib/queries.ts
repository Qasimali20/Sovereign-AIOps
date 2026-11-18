/**
 * Server state. One shared cache for the whole app: components that need the
 * same resource share a single request and a single poll. Polling pauses while
 * the tab is hidden (TanStack default), and mutations invalidate what they change.
 */
import { QueryClient, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, ApiError, type AuditQuery, type LlmRange, type Role, type Verdict } from './api'

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: (n, e) => !(e instanceof ApiError && e.status < 500) && n < 2,
      refetchOnWindowFocus: true,
      staleTime: 1_000,
    },
  },
})

export const keys = {
  me: ['me'] as const,
  fleet: ['fleet'] as const,
  history: (node: string, range: string) => ['history', node, range] as const,
  security: (h: number) => ['security', h] as const,
  overview: (days: number, origin: string) => ['overview', days, origin] as const,
  system: ['system'] as const,
  policy: ['policy'] as const,
  incidents: ['incidents'] as const,
  incident: (id: string) => ['incident', id] as const,
  approvals: (status: string) => ['approvals', status] as const,
  audit: (p: object) => ['audit', p] as const,
  users: ['users'] as const,
}

const SEC = 1_000

export const useFleet = () => useQuery({ queryKey: keys.fleet, queryFn: api.fleet, refetchInterval: 5 * SEC })
export const useNodeHistory = (node: string, range: string) =>
  useQuery({ queryKey: keys.history(node, range), queryFn: () => api.nodeHistory(node, range), refetchInterval: range === '15m' ? 10 * SEC : 60 * SEC })
export const useScorecard = (range: string) =>
  useQuery({ queryKey: ['scorecard', range], queryFn: () => api.scorecard(range), refetchInterval: 60 * SEC, placeholderData: prev => prev })
export const useModelBoard = (model: string, range: string) =>
  useQuery({ queryKey: ['model-board', model, range], queryFn: () => api.modelBoard(model, range),
    refetchInterval: range === '1h' ? 30 * SEC : 120 * SEC, placeholderData: prev => prev })
export const useMetricsBoard = (board: string, range: string) =>
  useQuery({ queryKey: ['metrics-board', board, range], queryFn: () => api.metricsBoard(board, range),
    refetchInterval: range === '1h' ? 30 * SEC : 120 * SEC, placeholderData: prev => prev })
export const useDeployments = () => useQuery({ queryKey: ['deployments'], queryFn: api.deployments, refetchInterval: 15 * SEC })
export const useDrift = () => useQuery({ queryKey: ['drift'], queryFn: api.drift, refetchInterval: 30 * SEC })
export function useDriftAccept() {
  const qc = useQueryClient()
  return useMutation({ mutationFn: (v: { node: string; keys: string[] | null; reason: string }) => api.driftAccept(v.node, v.keys, v.reason),
    onSuccess: () => ['drift', 'incidents', 'audit'].forEach(k => qc.invalidateQueries({ queryKey: [k] })) })
}
export function useDriftCheck() {
  const qc = useQueryClient()
  return useMutation({ mutationFn: api.driftCheck, onSuccess: () => qc.invalidateQueries({ queryKey: ['drift'] }) })
}
export const useServices = () => useQuery({ queryKey: ['services'], queryFn: api.services, refetchInterval: 15 * SEC })
export const useSecurity = (hours = 24) => useQuery({ queryKey: keys.security(hours), queryFn: () => api.security(hours), refetchInterval: 5 * SEC })
export function useRunLab() {
  const qc = useQueryClient()
  return useMutation({ mutationFn: api.runLab, onSuccess: () => { for (const k of [['security'], keys.incidents, ['overview']]) qc.invalidateQueries({ queryKey: k }) } })
}
export function useResetLab() {
  const qc = useQueryClient()
  return useMutation({ mutationFn: api.resetLab, onSuccess: () => qc.invalidateQueries({ queryKey: ['security'] }) })
}
export const useOverview = (days = 7, origin: 'real' | 'all' = 'real') =>
  useQuery({ queryKey: keys.overview(days, origin), queryFn: () => api.overview(days, origin), refetchInterval: 6 * SEC, placeholderData: p => p })
export const useSystem = () => useQuery({ queryKey: keys.system, queryFn: api.system, refetchInterval: 10 * SEC })
export const usePolicy = () => useQuery({ queryKey: keys.policy, queryFn: api.policy, staleTime: 5 * 60 * SEC })
export const useIncidents = (enabled = true) => useQuery({ queryKey: keys.incidents, queryFn: api.incidents, refetchInterval: 4 * SEC, enabled })
export const useIncidentSummary = () => useQuery({ queryKey: ['incident-summary'], queryFn: api.incidentSummary, refetchInterval: 5 * SEC })
export const useIncident = (id: string) =>
  useQuery({
    queryKey: keys.incident(id), queryFn: () => api.incident(id),
    // poll fast while the workflow is moving, slowly once it has settled
    refetchInterval: q => (q.state.data && !q.state.data.running && q.state.data.report ? 15 * SEC : 2.5 * SEC),
  })
export const useLlmSummary = (range: LlmRange) =>
  useQuery({ queryKey: ['llm-summary', range], queryFn: () => api.llmSummary(range), refetchInterval: 15 * SEC, placeholderData: p => p })
export const useLlmEnergy = (range: LlmRange) =>
  useQuery({ queryKey: ['llm-energy', range], queryFn: () => api.llmEnergy(range), refetchInterval: 60 * SEC, placeholderData: p => p })
export const useLlmRequests = (range: LlmRange, f: { model?: string; client?: string; outcome?: string }) =>
  useQuery({ queryKey: ['llm-requests', range, f], queryFn: () => api.llmRequests(range, f), refetchInterval: 10 * SEC, placeholderData: p => p })
export const useEvals = () => useQuery({ queryKey: ['evals'], queryFn: api.evals, refetchInterval: q => (q.state.data?.running ? 3 * SEC : 30 * SEC) })
export const useEvalHistory = (model: string) => useQuery({ queryKey: ['eval-history', model], queryFn: () => api.evalHistory(model), enabled: !!model })
export function useRunEvals() {
  const qc = useQueryClient()
  return useMutation({ mutationFn: api.runEvals, onSuccess: () => qc.invalidateQueries({ queryKey: ['evals'] }) })
}
export const usePerf = () => useQuery({ queryKey: ['perf'], queryFn: api.perf, refetchInterval: q => (q.state.data?.running ? 3 * SEC : 60 * SEC) })
export function useRunPerf() {
  const qc = useQueryClient()
  return useMutation({ mutationFn: api.runPerf, onSuccess: () => qc.invalidateQueries({ queryKey: ['perf'] }) })
}
export const useBottlenecks = (window: string) =>
  useQuery({ queryKey: ['bottlenecks', window], queryFn: () => api.bottlenecks(window), refetchInterval: 60 * SEC, placeholderData: prev => prev })
export const useTopology = () => useQuery({ queryKey: ['topology'], queryFn: api.topology, refetchInterval: 30 * SEC })
export const useImpact = (target: string | null) =>
  useQuery({ queryKey: ['impact', target], queryFn: () => api.impact(target!), enabled: !!target })
export const useTrends = () => useQuery({ queryKey: ['trends'], queryFn: api.trends, refetchInterval: 300 * SEC })
export const usePlacement = (gb?: number) =>
  useQuery({ queryKey: ['placement', gb], queryFn: () => api.placement(gb), refetchInterval: 300 * SEC, placeholderData: prev => prev })
export const useCapacity = () => useQuery({ queryKey: ['capacity'], queryFn: api.capacity, refetchInterval: 120 * SEC })
export const useRegistry = () => useQuery({ queryKey: ['registry'], queryFn: api.registry, refetchInterval: 60 * SEC })
export const useAgentOverview = () => useQuery({ queryKey: ['agent-overview'], queryFn: () => api.agentOverview(30), refetchInterval: 30 * SEC })
export const useAgentRuns = () => useQuery({ queryKey: ['agent-runs'], queryFn: api.agentRuns, refetchInterval: 15 * SEC })
export const useAgentRun = (id: string) => useQuery({ queryKey: ['agent-run', id], queryFn: () => api.agentRun(id), refetchInterval: 15 * SEC })
export function useRateDiagnosis(id: string) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (v: { verdict: Verdict; note: string }) => api.rateDiagnosis(id, v.verdict, v.note),
    onSuccess: () => ['agent-run', 'agent-runs', 'agent-overview'].forEach(k => qc.invalidateQueries({ queryKey: [k] })),
  })
}
export const useApprovals = (status: string) =>
  useQuery({ queryKey: keys.approvals(status), queryFn: () => api.approvals(status), refetchInterval: 4 * SEC })
export const useAudit = (p: AuditQuery) =>
  useQuery({ queryKey: keys.audit(p), queryFn: () => api.audit(p), refetchInterval: 6 * SEC })
export const useAuditIntegrity = () =>
  useQuery({ queryKey: ['audit-integrity'], queryFn: api.auditIntegrity, refetchInterval: 60 * SEC })
export const useUsers = (enabled = true) => useQuery({ queryKey: keys.users, queryFn: api.users, enabled })

/** Everything an incident action can change. */
function useInvalidateIncident() {
  const qc = useQueryClient()
  return (id?: string) => {
    if (id) qc.invalidateQueries({ queryKey: keys.incident(id) })
    for (const k of [keys.incidents, ['incident-summary'], ['approvals'], ['audit']]) qc.invalidateQueries({ queryKey: k })
  }
}

export function useDecide() {
  const inv = useInvalidateIncident()
  return useMutation({
    mutationFn: (v: { approvalId: string; incidentId: string; approve: boolean; comment: string }) =>
      api.decide(v.approvalId, v.approve, v.comment),
    onSuccess: (_, v) => inv(v.incidentId),
  })
}

export function useCloseIncident() {
  const inv = useInvalidateIncident()
  return useMutation({
    mutationFn: (v: { id: string; note: string; disposition?: string }) => api.closeIncident(v.id, v.note, v.disposition),
    onSuccess: (_, v) => inv(v.id),
  })
}

export function useRerunIncident() {
  const inv = useInvalidateIncident()
  return useMutation({ mutationFn: (id: string) => api.rerunIncident(id), onSuccess: (_, id) => inv(id) })
}

export function useScan() {
  const inv = useInvalidateIncident()
  return useMutation({ mutationFn: api.scan, onSuccess: () => inv() })
}

export function useSaveUser() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (v: { username: string; password: string; role: Role }) => api.saveUser(v.username, v.password, v.role),
    onSuccess: () => qc.invalidateQueries({ queryKey: keys.users }),
  })
}

export function useDeleteUser() {
  const qc = useQueryClient()
  return useMutation({ mutationFn: api.deleteUser, onSuccess: () => qc.invalidateQueries({ queryKey: keys.users }) })
}


export const useChangePassword = () =>
  useMutation({ mutationFn: (v: { current: string; next: string }) => api.changePassword(v.current, v.next) })
