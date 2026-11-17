/** Pure formatting helpers. No React, no I/O. */
const T = { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false } as const
export const clock = (t: number | null | undefined) => (t ? new Date(t * 1000).toLocaleTimeString([], T) : '—')
/** date + time without year, for tooltips (e.g. "16 Nov, 14:05") */
export const stamp = (t: number) => new Date(t * 1000).toLocaleString([], { hour12: false, month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
export const hm = (t: number) => new Date(t * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false })
export const dateTime = (t: number | null | undefined) =>
  t ? new Date(t * 1000).toLocaleString([], { month: 'short', day: 'numeric', ...T }) : '—'

export function ago(t: number) {
  const s = Math.max(0, Date.now() / 1000 - t)
  if (s < 60) return `${Math.floor(s)}s ago`
  if (s < 3600) return `${Math.floor(s / 60)}m ago`
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`
  return `${Math.floor(s / 86400)}d ago`
}

export function duration(s: number | null | undefined) {
  if (s == null) return '—'
  if (s < 60) return `${s.toFixed(s < 10 ? 1 : 0)}s`
  if (s < 3600) return `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`
  if (s < 86400) return `${Math.floor(s / 3600)}h ${Math.round((s % 3600) / 60)}m`
  return `${Math.floor(s / 86400)}d ${Math.round((s % 86400) / 3600)}h`
}

export type Tone = 'good' | 'warn' | 'serious' | 'crit' | 'info' | 'neutral'

export const STATUS_TONE: Record<string, Tone> = {
  open: 'info', investigating: 'info', executing: 'info', verifying: 'info',
  awaiting_approval: 'warn', needs_human: 'serious', diagnosed: 'warn',
  resolved: 'good', recovered: 'good', closed: 'neutral', action_rejected: 'neutral',
  not_resolved: 'crit', action_denied: 'crit',
}
export { FINAL } from './status'
export const label = (s: string) => s.replace(/_/g, ' ').replace(/^\w/, c => c.toUpperCase())
export const pct = (v: number | null | undefined, d = 0) => (v == null ? '—' : `${v.toFixed(d)}%`)
export const num = (v: number | null | undefined, d = 1) => (v == null ? '—' : v.toFixed(d))

/** What the agent proposed, in plain words. */
export function describeAction(op: string | null | undefined, target?: string | null): string {
  switch (op) {
    case 'restart_test_service': return `Restart ${target ?? 'the service'}`
    case 'clear_test_cache': return `Clear the cache of ${target ?? 'the service'}`
    case 'scale_test_service': return `Scale ${target ?? 'the service'} out`
    case 'rollback_test_deployment': return `Roll back ${target ?? 'the service'} to the previous release`
    case 'block_ip_lab': return `Block ${target ?? 'the source'} (lab firewall)`
    case 'disable_lab_account': return `Disable account ${target ?? ''} (lab)`.replace('  ', ' ')
    case 'isolate_lab_endpoint': return `Isolate ${target ?? 'the endpoint'} (lab network)`
    case 'quarantine_lab_file': return `Quarantine ${target ?? 'the file'} (lab)`
    case 'escalate_to_human': return 'Handed to a person, with steps'
    case 'none': return 'No action needed'
    default: return 'Still investigating'
  }
}

/** Incident outcome, in plain words. */
export const OUTCOME: Record<string, string> = {
  open: 'Investigating', investigating: 'Investigating', executing: 'Fixing', verifying: 'Checking the fix',
  awaiting_approval: 'Waiting for your approval', resolved: 'Fixed and verified', recovered: 'Recovered', closed: 'Closed by a person',
  diagnosed: 'Needs a person to follow up', needs_human: 'Needs a person', not_resolved: 'Fix did not work',
  action_rejected: 'You rejected the action', action_denied: 'Blocked by policy',
}

/** a latency in seconds: "84 ms" below a second, "2.31 s" above */
export const seconds = (v: number | null | undefined) => v == null ? '—' : v < 1 ? `${Math.round(v * 1000)} ms` : `${v.toFixed(v < 10 ? 2 : 1)} s`
/** a count with sensible precision: 1,234 · 56 · 3.25 */
export const count = (v: number | null | undefined) => v == null ? '—' : v >= 100 ? Math.round(v).toLocaleString() : v >= 10 ? v.toFixed(0) : v.toFixed(v < 1 && v > 0 ? 2 : 0)
/** a percentage with one decimal only below 10 */
export const percent = (v: number | null | undefined) => v == null ? '—' : `${v.toFixed(v < 10 && v > 0 ? 1 : 0)}%`
/** bytes per second in B/s … GB/s (decimal units) */
export function bytesPerSec(b: number | null | undefined, compact = false): string {
  if (b == null || Number.isNaN(b)) return '—'
  const u = ['B/s', 'KB/s', 'MB/s', 'GB/s']; let x = b, i = 0
  while (Math.abs(x) >= 1000 && i < u.length - 1) { x /= 1000; i++ }
  return compact ? `${+x.toFixed(x < 10 && i ? 1 : 0)}${['', 'K', 'M', 'G'][i]}` : `${x.toFixed(x < 10 && i ? 1 : 0)} ${u[i]}`
}
/** CSS severity class for a value against warn/crit thresholds ('' when fine or unknown) */
export const severity = (v: number | null | undefined, warn: number, crit: number) => v == null ? '' : v >= crit ? 'crit' : v >= warn ? 'warn' : ''
export const ENGINE: Record<string, string> = { vllm: 'vLLM', 'vllm-omni': 'vLLM-Omni', sglang: 'SGLang', ollama: 'Ollama' }

/** Uptime in hours as days and whole hours: 44.9 -> "1 d 20 h", 5.2 -> "5 h", 0.4 -> "24 min". */
export function uptime(hours: number | null | undefined): string {
  if (hours == null || Number.isNaN(hours)) return '—'
  if (hours < 1) return `${Math.max(1, Math.round(hours * 60))} min`
  const d = Math.floor(hours / 24), h = Math.floor(hours % 24)
  return d ? `${d} d${h ? ` ${h} h` : ''}` : `${h} h`
}
