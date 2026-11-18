/* One metric-history panel (line / stacked / timeline / bars / donut) and its value formatting.
 * Shared by the Metric history boards and the per-model pages. */
import { useMemo } from 'react'
import type { BoardPanel } from '../lib/api'
import { Donut, SLOTS, TimeChart } from './charts'
import { FillBars, Timeline } from './viz'
import { Card, Empty } from './ui'
import { bytesPerSec, label } from '../lib/format'

export type Range = '1h' | '6h' | '24h' | '7d' | '30d'
export const RANGES: { key: Range; label: string }[] = [
  { key: '1h', label: '1 hour' }, { key: '6h', label: '6 hours' }, { key: '24h', label: '24 hours' },
  { key: '7d', label: '7 days' }, { key: '30d', label: '30 days' },
]
const DOMAIN: Record<string, string> = { sre: 'Operations (SRE)', security: 'Security' }
/* outcome colours carry meaning (green = fixed, orange = waiting on a person), so they are not the categorical slots */
export const STATUS_COLOR: Record<string, string> = {
  resolved: 'var(--good)', recovered: 'var(--s1)', awaiting_approval: 'var(--warn)', action_rejected: '#8b8478',
  closed: '#5f5a51', open: 'var(--s2)', diagnosed: 'var(--s3)', needs_human: 'var(--s4)', not_resolved: 'var(--crit)', action_denied: 'var(--crit)', investigating: 'var(--s4)', executing: 'var(--s4)', verifying: 'var(--s4)', failed: 'var(--crit)',
  sre: 'var(--s1)', security: 'var(--s2)',
  Services: 'var(--s1)', Models: 'var(--s2)', GPU: 'var(--s3)', Security: 'var(--s4)', Nodes: 'var(--m-disk)', Configuration: 'var(--m-mem)', Other: 'var(--muted)',
}

export function fmt(unit: string, v: number | null | undefined, compact = false): string {
  if (v == null || Number.isNaN(v)) return '—'
  const a = Math.abs(v)
  switch (unit) {
    case 'percent': return `${v.toFixed(a < 10 && !compact ? 1 : 0)}%`
    case 'celsius': return `${v.toFixed(0)}°C`
    case 'watt': return `${v.toFixed(a < 10 ? 1 : 0)} W`
    case 'Bps': return bytesPerSec(v, compact)
    case 's': {
      const sp = compact ? '' : ' '
      if (a >= 86400) return `${(v / 86400).toFixed(1)}${sp}d`
      if (a >= 3600) return `${(v / 3600).toFixed(1)}${sp}h`
      if (a >= 60) return `${+(v / 60).toFixed(1)}${sp}m`
      if (a >= 1 || a === 0) return `${v.toFixed(a >= 10 || compact ? 0 : 1)}${sp}s`
      return `${(v * 1000).toFixed(0)}${sp}ms`
    }
    case 'reqps': return `${v.toFixed(a < 1 ? 2 : 1)}/s`
    case 'rpm': return compact ? (a >= 10 || a === 0 ? v.toFixed(0) : v.toFixed(1)) : `${v.toFixed(a >= 10 || a === 0 ? 0 : 1)}/min`
    case 'x': return `${v.toFixed(2)}×`
    case 'tps': return compact ? v.toFixed(a < 10 ? 1 : 0) : `${v.toFixed(a < 10 ? 1 : 0)} tok/s`
    default: return Number.isInteger(v) ? String(v) : v.toFixed(a < 10 ? 1 : 0)
  }
}

function xFmtFor(range: Range) {
  if (range === '1h' || range === '6h' || range === '24h')
    return (t: number) => new Date(t * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false })
  return (t: number) => new Date(t * 1000).toLocaleDateString([], { day: 'numeric', month: 'short' })
}

export function Panel({ p, range, span, onSelect }: { p: BoardPanel; range: Range; span: [number, number]; onSelect?: (key: string) => void }) {
  const data = useMemo(() => p.points.map(row => {
    const o: Record<string, number | null> = { t: row[0] as number }
    p.series.forEach((s, i) => { o[s.key] = row[i + 1] })
    return o
  }), [p])
  const series = p.series.map((s, i) => ({
    key: s.key, name: p.by === 'domain' ? (DOMAIN[s.name] ?? label(s.name)) : p.by === 'status' ? label(s.name) : s.name, fmt: (v: number) => fmt(p.unit, v),
    color: s.slot == null ? 'var(--muted)' : SLOTS[s.slot % SLOTS.length] ?? SLOTS[i % SLOTS.length],
  }))
  const hint = p.hidden.length ? `top ${p.series.length} of ${p.series.length + p.hidden.length}` : ''
  const empty = !data.length || !series.length
  const rows = p.series.map((s, i) => ({ name: s.name, values: p.points.map(r => r[i + 1]) }))
  const times = p.points.map(r => r[0] as number)
  const step = times.length > 1 ? times[1] - times[0] : 60
  let body
  if (p.error) body = <Empty>Prometheus did not answer for this chart.</Empty>
  else if (empty) body = <Empty>No data in this range.</Empty>
  else if (p.kind === 'timeline') body = <Timeline times={times} rows={rows} events={p.events} xFmt={xFmtFor(range)} step={step} />
  else if (p.kind === 'donut') {
    const latest = rows.map(r => [...r.values].reverse().find(v => v != null) ?? 0)
    const items = rows.map((r, i) => ({ name: series[i].name, key: r.name, value: latest[i], color: STATUS_COLOR[r.name] ?? series[i].color }))
      .filter(i => i.value > 0).sort((a, b) => b.value - a.value)
    body = items.length ? <Donut items={items} centerLabel="incidents" onSelect={onSelect} /> : <Empty>None right now.</Empty>
  }
  else if (p.kind === 'bars') body = <FillBars rows={rows} fmt={v => fmt(p.unit, v)} warn={p.warn} />
  else body = <TimeChart data={data} series={series} height={200} xFmt={xFmtFor(range)} xDomain={span} onSelect={onSelect}
    stacked={p.kind === 'stacked'} warn={p.warn} warnLabel={p.warn_label}
    yDomain={[0, p.max ?? 'auto']} yFmt={v => fmt(p.unit, v, true)} />
  return <Card title={<span title={p.desc || undefined}>{p.title}</span>} hint={hint || undefined} className={['line', 'stacked', 'donut'].includes(p.kind) ? '' : 'span-2'}>{body}</Card>
}

