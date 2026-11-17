import { clock } from '../lib/format'
import { useState } from 'react'
import type { LogEntry, LogResult } from '../lib/api'


const SEV: Record<string, string> = {
  emerg: 'crit', alert: 'crit', crit: 'crit', critical: 'crit', fatal: 'crit', err: 'crit', error: 'crit',
  warning: 'warn', notice: 'info', info: 'info', debug: 'debug',
}
const sevLabel = (s: string) => ({ err: 'error', crit: 'critical', emerg: 'emergency' } as Record<string, string>)[s] ?? s


export function LogTable({ entries, showService = true, max = 400 }: { entries: LogEntry[]; showService?: boolean; max?: number }) {
  if (!entries.length) return <div className="log-empty">No log lines in this window.</div>
  return (
    <div className="logs" role="table">
      {entries.slice(0, max).map((e, i) => (
        <div key={i} className={`log-row ${SEV[e.severity] ?? 'info'}`} role="row">
          <span className="log-t mono">{clock(e.ts)}</span>
          <span className={`log-sev ${SEV[e.severity] ?? 'info'}`}>{sevLabel(e.severity) || 'info'}</span>
          {showService && <span className="log-svc mono" title={e.service}>{e.service}</span>}
          <span className="log-line mono">{e.line}</span>
        </div>
      ))}
    </div>
  )
}

export function LogEvidence({ data }: { data: LogResult }) {
  const services = Object.entries(data.lines_by_service ?? {}).sort((a, b) => b[1] - a[1])
  return (
    <div className="stack" style={{ gap: 10 }}>
      <div className="ev-meta">
        <code>{data.query}</code>
        <span>{clock(data.window.start)}–{clock(data.window.end)}</span>
        <span>{data.returned}{data.truncated ? '+' : ''} lines</span>
        {services.slice(0, 6).map(([s, n]) => <span key={s} className="chip">{s} · {n}</span>)}
      </div>
      <LogTable entries={data.entries} showService={services.length > 1} />
    </div>
  )
}

type Summary = { min: number; max: number; avg: number; last: number; points: [string, number][] }
type MetricWindow = { node: string; window: { start: number; end: number; step_s: number }; series: Record<string, Record<string, Summary> | string>; errors: string[] }

const LABEL: Record<string, [string, string]> = {
  gpu_util_percent: ['GPU', '%'], cpu_percent: ['CPU', '%'], memory_used_percent: ['Memory', '%'], node_up: ['Reachable', ''],
  model_requests_running: ['Running', ''], model_requests_waiting: ['Waiting', ''], model_requests_per_min: ['Requests', '/min'],
  model_ttft_p95_s: ['First token p95', 's'], model_e2e_p95_s: ['Whole request p95', 's'], model_kv_cache_percent: ['KV cache', '%'],
}

function Spark({ pts }: { pts: [string, number][] }) {
  if (pts.length < 2) return <svg className="spark" />
  const v = pts.map(p => p[1]); const lo = Math.min(...v), hi = Math.max(...v), r = hi - lo
  const d = v.map((x, i) => `${i ? 'L' : 'M'}${(i / (v.length - 1)) * 100},${r ? 22 - ((x - lo) / r) * 20 : 12}`).join(' ')
  return <svg className="spark" viewBox="0 0 100 24" preserveAspectRatio="none"><path d={d} /></svg>
}

const fmt = (x: number, unit: string) => `${Math.abs(x) >= 100 ? Math.round(x) : +x.toFixed(x < 1 && x > 0 ? 3 : 1)}${unit === '%' ? '%' : unit ? ' ' + unit : ''}`

export function MetricEvidence({ data }: { data: MetricWindow }) {
  const rows: { metric: string; who: string; s: Summary }[] = []
  for (const [k, v] of Object.entries(data.series)) {
    if (typeof v === 'string') continue
    for (const [who, s] of Object.entries(v)) rows.push({ metric: k, who, s })
  }
  return (
    <div className="stack" style={{ gap: 10 }}>
      <div className="ev-meta"><span>Prometheus · {data.node}</span><span>{clock(data.window.start)}–{clock(data.window.end)} · {data.window.step_s}s steps</span>
        {data.errors.length > 0 && <span className="crit">{data.errors.length} queries failed</span>}</div>
      <div className="table-wrap">
        <table className="table mtable">
          <thead><tr><th>Metric</th><th>Series</th><th className="num">Min</th><th className="num">Avg</th><th className="num">Max</th><th className="num">Last</th><th>Trend</th></tr></thead>
          <tbody>
            {rows.map(({ metric, who, s }) => {
              const [l, u] = LABEL[metric] ?? [metric, '']
              return (
                <tr key={metric + who}>
                  <td>{l}</td><td className="mono small">{who}</td>
                  <td className="num">{fmt(s.min, u)}</td><td className="num">{fmt(s.avg, u)}</td>
                  <td className="num"><b>{fmt(s.max, u)}</b></td><td className="num">{fmt(s.last, u)}</td>
                  <td><Spark pts={s.points} /></td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}

/** Evidence body: a purpose-built view for logs and metric windows, raw JSON for everything else (and on request). */
export function EvidenceBody({ tool, data }: { tool: string; data: unknown }) {
  const [raw, setRaw] = useState(false)
  const d = data as Record<string, unknown>
  const err = d && typeof d === 'object' && 'error' in d && Object.keys(d).length === 1
  const view = !err && (tool.endsWith('_logs') && tool !== 'get_container_logs' && 'entries' in d ? <LogEvidence data={d as unknown as LogResult} />
    : tool === 'get_metric_window' && 'series' in d ? <MetricEvidence data={d as unknown as MetricWindow} /> : null)
  if (!view || raw) return (
    <div>
      {view && <button className="btn btn-sm btn-ghost ev-toggle" onClick={() => setRaw(false)}>Formatted</button>}
      <pre className="json">{JSON.stringify(data, null, 2)}</pre>
    </div>
  )
  return (
    <div className="ev-body">
      <button className="btn btn-sm btn-ghost ev-toggle" onClick={() => setRaw(true)}>Raw</button>
      {view}
    </div>
  )
}
