/* Incident status semantics, in one place (mirrors backend/core/store.py FINAL_STATUSES). */
export const FINAL = ['resolved', 'recovered', 'closed', 'action_rejected'] as const

export const isFinal = (s: string) => (FINAL as readonly string[]).includes(s)
export const isActive = (s: string) => !isFinal(s)
/** an action reached the executor (or is past it) */
export const isExecuted = (s: string) => ['resolved', 'not_resolved', 'executing', 'verifying'].includes(s)

/** What an incident kind is about. Mirrors CATEGORY_SQL in backend/core/analytics.py (the dashboard's category chart). */
export function categoryOf(kind: string): string {
  if (kind.startsWith('sec_')) return 'Security'
  if (kind.startsWith('model_')) return 'Models'
  if (kind.startsWith('gpu_')) return 'GPU'
  if (kind.startsWith('svc_') || kind === 'service_unhealthy') return 'Services'
  if (kind === 'config_drift') return 'Configuration'
  if (['node_unreachable', 'node_rebooted', 'high_cpu', 'high_memory'].includes(kind) || /^(disk|net|capacity)_/.test(kind)) return 'Nodes'
  return 'Other'
}

export const ORIGIN_LABEL: Record<string, string> = { drill: 'Drill', planned: 'Planned work', merged: 'Merged duplicate' }

/** how an outcome should read: good (fixed/cleared), crit (fix failed or refused), info (closed by a person), warn (other) */
export function outcomeTone(s: string): 'good' | 'crit' | 'info' | 'warn' {
  if (s === 'resolved' || s === 'recovered') return 'good'
  if (s === 'not_resolved' || s === 'action_denied') return 'crit'
  return s === 'closed' ? 'info' : 'warn'
}

export const VERDICT_LABEL: Record<'correct' | 'partly' | 'wrong', string> = { correct: 'Correct', partly: 'Partly right', wrong: 'Wrong' }
