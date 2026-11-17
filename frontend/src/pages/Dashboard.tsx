import { Link, useNavigate } from 'react-router-dom'
import { AlertTriangle, CheckCircle2, Loader2 } from 'lucide-react'
import type { Overview } from '../lib/api'
import { useMetricsBoard, useOverview } from '../lib/queries'
import { Panel, STATUS_COLOR } from '../components/MetricPanel'
import { Donut, SLOTS, TimeChart } from '../components/charts'
import { Card, Empty, Skeleton, StatusBadge } from '../components/ui'
import { Tile } from '../components/Tile'
import { ago, describeAction, duration, label } from '../lib/format'
import { isExecuted as executed } from '../lib/status'

/* The overview answers four questions, most urgent first:
 * 1. Do I need to do anything?   2. Are the servers healthy?
 * 3. What has the AI been doing?  4. Is it working well and safely?
 * Detail (pipeline, guardrails, test record) lives on the Policies page. */

function Attention({ o }: { o: Overview | undefined }) {
  if (!o) return null

  const { in_progress, needs_person } = o.active
  if (needs_person) {
    return (
      <section className="attention warn">
        <div className="attention-head"><AlertTriangle size={18} />
          <div><h2>{needs_person} {needs_person === 1 ? 'incident needs' : 'incidents need'} a person</h2>
            <span>The AI diagnosed {needs_person === 1 ? 'it' : 'them'} but cannot act safely on its own. <Link to="/incidents">Review</Link></span></div></div>
      </section>
    )
  }
  if (in_progress) {
    return (
      <section className="attention info">
        <div className="attention-head"><Loader2 size={18} className="spin" />
          <div><h2>The AI is working on {in_progress} {in_progress === 1 ? 'incident' : 'incidents'}</h2>
            <span>You will be asked here if an action needs your approval.</span></div></div>
      </section>
    )
  }
  return null                                   // nothing needs attention: say nothing
}

const MEM_WARN = 92   // same threshold as the detector's high-memory rule

/* One line per node: healthy nodes just say so; a node with problems names them. Detail lives on Fleet. */
function problems(n: Overview['fleet']['nodes'][string]): string[] {
  if (!n.ok) return ['not responding']
  const sv = n.services, out: string[] = []
  const svcBad = (sv.containers_unhealthy ?? 0) + (sv.containers_restarting ?? 0) + (sv.units_failed ?? 0)
  if (n.models_ready < n.models) out.push(`${n.models - n.models_ready} of ${n.models} models not ready`)
  if (svcBad) out.push(`${svcBad} ${svcBad === 1 ? 'service' : 'services'} failing`)
  if (!sv.ok) out.push('services not readable')
  if ((n.mem_pct ?? 0) >= MEM_WARN) out.push(`memory ${n.mem_pct!.toFixed(0)}%`)
  if ((n.disk_pct ?? 0) >= 85) out.push(`disk ${n.disk_pct!.toFixed(0)}%`)
  if (n.kernel_ok === false) out.push('GPU driver log unreadable')
  return out
}

function FleetStatus({ f }: { f: Overview['fleet'] }) {
  return (
    <div className="fleet-status fleet-list">
      {Object.entries(f.nodes).map(([name, n]) => {
        const p = problems(n)
        const state = !n.ok ? 'down' : p.length ? 'warning' : 'healthy'
        return (
          <Link key={name} to={`/fleet/${name}`} className={`fs-node ${state}`}>
            <span className={`server-state ${state}`}><i /></span>
            <b>{name}</b>
            <span className="fs-text">{p.length ? p.join(' · ') : `Healthy · ${n.models_ready} models running`}</span>
          </Link>
        )
      })}
    </div>
  )
}

// A write operation only counts as done once the executor ran it; otherwise it was a proposal.
const isWrite = (op?: string | null) => !!op && !['none', 'escalate_to_human'].includes(op)

function Recent({ o }: { o: Overview }) {
  const nav = useNavigate()
  if (!o.recent.length) return <div className="empty-inline"><CheckCircle2 size={18} />No incidents yet.</div>
  return (
    <table className="table click">
      <thead><tr><th>What happened</th><th>What the AI did</th><th>Outcome</th></tr></thead>
      <tbody>
        {o.recent.slice(0, 6).map(i => (
          <tr key={i.id} onClick={() => nav(`/incidents/${i.id}`)}>
            <td><Link className="primary row-link" to={`/incidents/${i.id}`} onClick={e => e.stopPropagation()}>{i.title}</Link><div className="sub">{i.node} · {ago(i.created)}</div></td>
            <td className="ink2">{executed(i.status) || !isWrite(i.operation) ? '' : 'Proposed: '}{describeAction(i.operation, i.target)}</td>
            <td className="nowrap"><StatusBadge status={i.status} /></td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

/** Opened vs fixed per day: whether the AI keeps up with what the detector finds. */
function Daily({ o, onSelect }: { o: Overview; onSelect: () => void }) {
  const day = (t: number) => new Date(t * 1000).toLocaleDateString([], { weekday: 'short', day: 'numeric' })
  return (
    <Card title="Incidents per day" hint={`last ${o.window_days} days`}>
      <TimeChart data={o.daily.map(d => ({ t: d.t, opened: d.opened, resolved: d.resolved }))} height={200} area xFmt={day} onSelect={onSelect}
        yFmt={v => String(Math.round(v))} yDomain={[0, 'auto']}
        series={[{ key: 'opened', name: 'Opened', color: 'var(--s1)', fmt: v => String(v) },
                 { key: 'resolved', name: 'Fixed and verified', color: 'var(--good)', fmt: v => String(v) }]} />
    </Card>
  )
}

/** Window counts as a ring; a segment opens the matching incidents. */
function Breakdown({ title, counts, param, origin }: { title: string; counts: Record<string, number>; param: 'status' | 'category'; origin: string }) {
  const nav = useNavigate()
  const items = Object.entries(counts).map(([k, v], i) => ({ key: k, name: param === 'status' ? label(k) : k, value: v, color: STATUS_COLOR[k] ?? SLOTS[i % SLOTS.length] }))
  return (
    <Card title={title}>
      {items.length ? <Donut items={items} centerLabel="incidents" onSelect={k => nav(`/incidents?scope=all${origin === 'all' ? '&origin=all' : ''}&${param}=${encodeURIComponent(k)}`)} />
        : <Empty>No incidents in this window.</Empty>}
    </Card>
  )
}

/** Incident charts (from the overview, so they follow the Real / All switch) and fleet charts (24 h, Prometheus). */
function Activity({ o, origin }: { o: Overview | undefined; origin: string }) {
  const { data: b } = useMetricsBoard('operations', '24h')
  const nav = useNavigate()
  // every chart leads somewhere: a node's series to that node
  const to: Record<string, (k: string) => string> = {
    models_ready: k => `/fleet/${encodeURIComponent(k)}`,
    unhealthy: k => `/services#${encodeURIComponent(k)}`,
  }
  const panel = (id: string) => b?.panels.find(p => p.id === id)
  const span: [number, number] = b ? [b.start, b.end] : [0, 0]
  const show = (id: string) => {
    const p = panel(id)
    return p ? <Panel key={id} p={p} range="24h" span={span} onSelect={to[id] && (k => nav(to[id](k)))} /> : <Card key={id}><Skeleton h={200} /></Card>
  }
  return (
    <>
      <div className="grid g-3">
        {o ? <>
          <Daily o={o} onSelect={() => nav(`/incidents?scope=all${origin === 'all' ? '&origin=all' : ''}`)} />
          <Breakdown title="Incidents by outcome" counts={o.by_status} param="status" origin={origin} />
          <Breakdown title="Incidents by category" counts={o.by_category} param="category" origin={origin} />
        </> : [0, 1, 2].map(i => <Card key={i}><Skeleton h={200} /></Card>)}
      </div>
      <div className="grid g-2">
        {show('models_ready')}
        {show('unhealthy')}
      </div>
    </>
  )
}

/** The numbers that matter, one tile each (7-day window; the safety record is all-time). */
function Kpis({ o }: { o: Overview | undefined }) {
  if (!o) return <div className="ftiles six">{Array.from({ length: 6 }, (_, i) => <div key={i} className="ftile"><Skeleton h={50} /></div>)}</div>
  const p = o.pipeline, t = o.timing, g = o.guardrails, q = o.quality
  const evidence = q?.evidence_support_mean == null ? null : q.evidence_support_mean * 100
  const approved = g.executions_total - g.executions_without_human_approval
  const safe = g.executions_without_human_approval === 0
  return (
    <div className="ftiles six">
      <Tile i={0} label={o.real_only ? 'Real incidents' : 'Incidents'} color="var(--m-gpu)" color2="var(--m-gpu-2)" value={p.detected}
        foot={`last ${o.window_days} days · ${o.active.active} open now`} />
      <Tile i={1} label="Self-recovered" color="var(--s3)" value={p.recovered ?? 0} foot="the detector saw it clear" />
      <Tile i={2} label="Fixed & verified" color="var(--good)" value={p.verified.resolved} foot="after a person approved" />
      <Tile i={3} label="Diagnosis time" color="var(--m-mem)" value={t.detect_to_diagnosis_s ?? null} fmt={duration} foot="median, from alert" />
      <Tile i={4} label="Evidence-backed" color="var(--s4)" value={evidence} unit="%" pct={evidence} foot="conclusions citing evidence" />
      <Link to="/governance?tab=policies" className="ftile-link" aria-label="Human-approved actions: see the policies">
        <Tile i={5} label="Human-approved" color={!g.executions_total || safe ? 'var(--good)' : 'var(--crit)'} color2={!g.executions_total || safe ? 'var(--good-2)' : undefined}
          value={g.executions_total ? approved : null} of={g.executions_total || undefined}
          pct={g.executions_total ? (100 * approved) / g.executions_total : undefined}
          foot={g.executions_total ? 'actions run · all time, drills incl.' : 'no actions run yet'} />
      </Link>
    </div>
  )
}

export default function Dashboard() {
  const origin = 'all'                     // real-only view (useOverview(7, 'real')) is hidden for now
  const { data: o } = useOverview(7, origin)
  const shown = o
  return (
    <main className="content">
      <div className="page-head"><h1>Overview</h1></div>

      <Kpis o={shown} />

      <Attention o={o} />

      <Activity o={shown} origin={origin} />

      <div className="grid g-main">
        <Card title="Recent incidents"
          right={<Link to="/incidents?scope=all" className="btn btn-sm">All incidents</Link>} bodyClass="card-body tight">
          {o ? <Recent o={o} /> : <Skeleton h={220} />}
        </Card>
        <Card title="Servers" right={<Link to="/fleet" className="btn btn-sm">Fleet</Link>} bodyClass="card-body tight">
          {o ? <FleetStatus f={o.fleet} /> : <Skeleton h={220} />}
        </Card>
      </div>
    </main>
  )
}
