import { stamp } from '../lib/format'
/* Non-line chart forms for metric history:
 *  Heatmap   one row per node, one cell per time step; sequential single hue (GPU blue), darker = higher.
 *  Timeline  one row per node; reporting / not reporting / no data runs, with restart markers.
 *  FillBars  current fill per node with the alert threshold marked and the change over the range.
 * All share the hover model: a column highlight plus a tooltip with every row's value at that time. */
import { useMemo, useRef, useState } from 'react'

type Row = { name: string; values: (number | null)[] }

function useHover() {
  const ref = useRef<HTMLDivElement>(null)
  const [col, setCol] = useState<number | null>(null)
  const [x, setX] = useState(0)
  const onMove = (e: React.MouseEvent, n: number) => {
    const el = ref.current?.querySelector('.vz-cells') as HTMLElement | null
    if (!el) return
    const r = el.getBoundingClientRect()
    const f = (e.clientX - r.left) / r.width
    if (f < 0 || f > 1) { setCol(null); return }
    setCol(Math.min(n - 1, Math.floor(f * n)))
    setX(e.clientX - (ref.current!.getBoundingClientRect().left))
  }
  return { ref, col, x, onMove, clear: () => setCol(null) }
}

function Axis({ times, fmt }: { times: number[]; fmt: (t: number) => string }) {
  if (times.length < 2) return null
  const ticks = [0, 0.25, 0.5, 0.75, 1].map(f => times[Math.min(times.length - 1, Math.round(f * (times.length - 1)))])
  return <div className="vz-axis"><span />{<div>{ticks.map((t, i) => <span key={i}>{fmt(t)}</span>)}</div>}</div>
}


export function Timeline({ times, rows, events, xFmt, step }: {
  times: number[]; rows: Row[]; events: { series: number; t: number }[]; xFmt: (t: number) => string; step: number
}) {
  const h = useHover()
  const n = times.length
  const t0 = times[0], t1 = times[n - 1] + step
  const pos = (t: number) => `${(100 * (t - t0)) / (t1 - t0)}%`
  const runs = useMemo(() => rows.map(r => {
    const out: { s: number; e: number; state: 'up' | 'down' | 'none' }[] = []
    r.values.forEach((v, i) => {
      const state = v == null ? 'none' : v >= 0.5 ? 'up' : 'down'
      const last = out[out.length - 1]
      if (last && last.state === state) last.e = i + 1
      else out.push({ s: i, e: i + 1, state })
    })
    return out
  }), [rows])
  const avail = rows.map(r => {
    const known = r.values.filter(v => v != null) as number[]
    return known.length ? (100 * known.filter(v => v >= 0.5).length) / known.length : null
  })
  return (
    <div className="vz" ref={h.ref} onMouseMove={e => h.onMove(e, n)} onMouseLeave={h.clear}>
      {rows.map((r, ri) => (
        <div key={r.name} className="vz-row">
          <span className="vz-name">{r.name}</span>
          <div className="vz-cells tl">
            {runs[ri].map((u, i) => (
              <i key={i} className={`run ${u.state}`} style={{ left: `${(100 * u.s) / n}%`, width: `${(100 * (u.e - u.s)) / n}%` }} />
            ))}
            {events.filter(e => e.series === ri).map((e, i) => (
              <b key={i} className="restart" style={{ left: pos(e.t) }} title={`Restarted around ${stamp(e.t)}`} />
            ))}
            {h.col != null && <span className="cursor" style={{ left: `${(100 * (h.col + 0.5)) / n}%` }} />}
          </div>
          <span className="vz-val">{avail[ri] == null ? '—' : `${avail[ri]!.toFixed(avail[ri]! < 99.95 && avail[ri]! > 99 ? 2 : 1)}%`}</span>
        </div>
      ))}
      <Axis times={times} fmt={xFmt} />
      <div className="vz-legend">
        <span><i className="run up" />Reporting</span><span><i className="run down" />Not reporting</span>
        <span><i className="run none" />No data</span><span><b className="restart static" />Restart</span>
        <span className="muted">right: share of time reporting</span>
      </div>
      {h.col != null && (
        <div className="chart-tip vz-tip" style={{ left: h.x }}>
          <div className="tt">{stamp(times[h.col])}</div>
          {rows.map(r => {
            const v = r.values[h.col!]
            return <div key={r.name} className="tr">{r.name}<b>{v == null ? 'no data' : v >= 0.5 ? 'reporting' : 'down'}</b></div>
          })}
        </div>
      )}
    </div>
  )
}

export function FillBars({ rows, fmt, warn, unitDelta = 'pts' }: {
  rows: Row[]; fmt: (v: number) => string; warn: number | null; unitDelta?: string
}) {
  return (
    <div className="fillbars">
      {rows.map(r => {
        const known = r.values.filter(v => v != null) as number[]
        const now = known[known.length - 1], first = known[0]
        const d = now != null && first != null ? now - first : null
        const lvl = now == null || warn == null ? '' : now >= warn ? 'crit' : now >= warn - 10 ? 'warn' : ''
        return (
          <div key={r.name} className="fb-row">
            <span className="vz-name">{r.name}</span>
            <div className="fb-track">
              <i className={lvl} style={{ width: `${Math.min(100, now ?? 0)}%` }} />
              {warn != null && <b style={{ left: `${warn}%` }} title={`Incident at ${fmt(warn)}`} />}
            </div>
            <span className="fb-now">{now == null ? '—' : fmt(now)}</span>
            <span className={`fb-d ${d != null && d > 0.05 ? 'up' : ''}`}>
              {d == null ? '' : Math.abs(d) < 0.05 ? 'no change' : `${d > 0 ? '+' : '−'}${Math.abs(d).toFixed(1)} ${unitDelta}`}
            </span>
          </div>
        )
      })}
      {warn != null && <div className="vz-legend"><span><b className="fb-mark" />incident at {fmt(warn)}</span><span className="muted">right: change over the selected range</span></div>}
    </div>
  )
}
