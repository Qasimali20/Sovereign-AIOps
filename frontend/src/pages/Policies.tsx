import { Fragment } from 'react'
import { useOverview, usePolicy, useSystem } from '../lib/queries'
import { Guardrails, Pipeline } from '../components/assurance'
import { Badge, Card, PageHead, PolicyBadge, Skeleton , Page } from '../components/ui'
import { duration, label } from '../lib/format'

const LEVELS = [
  { n: 0, name: 'Observe', text: 'Collect telemetry and raise alerts.' },
  { n: 1, name: 'Analyse', text: 'Investigate and summarise evidence.' },
  { n: 2, name: 'Recommend', text: 'Propose actions without executing them.' },
  { n: 3, name: 'Approved execution', text: 'Run an allowlisted action after a human approves it.' },
  { n: 4, name: 'Bounded automation', text: 'Run narrowly defined low-risk actions without approval.' },
]

const THRESHOLDS: Record<string, (v: number) => string> = {
  scan_seconds: v => `every ${v}s`, mem_pct_warn: v => `≥ ${v}%`, disk_pct_warn: v => `≥ ${v}%`,
  gpu_temp_warn: v => `≥ ${v}°C`, model_loading_max_s: v => `> ${duration(v)}`, node_unreachable_after: v => `${v} failed polls`,
}

export default function Policies({ embedded = false }: { embedded?: boolean }) {
  const { data: p } = usePolicy()
  const { data: sys } = useSystem()
  const { data: o } = useOverview(7, 'all')

  return (
    <Page embedded={embedded}>
      <PageHead embedded={embedded} title="Policies & safety" />

      <Card title="How every incident flows" hint="last 7 days">
        {o ? <Pipeline p={o.pipeline} /> : <Skeleton h={110} />}
      </Card>
      {o ? <Guardrails o={o} /> : <Card><Skeleton h={260} /></Card>}

      <Card title="Autonomy">
        <div className="grid g-3" style={{ gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))' }}>
          {LEVELS.map(l => {
            const level = p?.autonomy.level ?? 0
            const on = l.n <= level, top = l.n === level
            return (
              <div key={l.n} className="card" style={{ padding: 14, borderColor: top ? 'var(--warn-line)' : undefined, opacity: on ? 1 : 0.45 }}>
                <div className="row"><Badge tone={top ? 'warn' : on ? 'good' : 'neutral'}>Level {l.n}</Badge></div>
                <div style={{ fontWeight: 600, marginTop: 8 }}>{l.name}</div>
                <div className="muted small">{l.text}</div>
                {top && p && <div className="small" style={{ marginTop: 6, color: 'var(--warn-ink)' }}>{p.autonomy.scope}</div>}
                {!on && <div className="small muted" style={{ marginTop: 6 }}>Not enabled</div>}
              </div>
            )
          })}
        </div>
      </Card>

      <div className="grid g-2">
        <Card title="Principles">
          {p ? <ol style={{ margin: 0, paddingLeft: 20, display: 'grid', gap: 8 }}>{p.rules.map(r => <li key={r}>{r}</li>)}</ol> : <Skeleton h={150} />}
        </Card>
        <Card title="Operation catalogue" bodyClass="card-body tight">
          {p ? (
            <table className="table">
              <thead><tr><th>Operation</th><th>Kind</th><th>Risk</th><th>Policy</th></tr></thead>
              <tbody>{p.operations.map(o => (
                <tr key={o.operation}><td><code>{o.operation}</code></td><td className="ink2">{o.kind}</td><td className="ink2">{o.risk}</td><td><PolicyBadge decision={o.decision} /></td></tr>
              ))}</tbody>
            </table>
          ) : <Skeleton h={120} />}
        </Card>
      </div>

      <Card title="Registered resources" bodyClass="card-body tight">
        {p ? (
          <div className="table-wrap">
            <table className="table">
              <thead><tr><th>Resource</th><th>Type</th><th>Node</th><th>Container</th><th>Allowed operations</th></tr></thead>
              <tbody>{p.resources.map(r => (
                <tr key={r.resource_id}>
                  <td className="primary">{r.resource_id}</td><td>{r.resource_type}</td><td>{r.node}</td>
                  <td className="mono small">{r.container}</td>
                  <td>{r.allowed_operations.map(o => <code key={o}>{o}</code>)}</td>
                </tr>
              ))}</tbody>
            </table>
          </div>
        ) : <Skeleton h={80} />}
      </Card>

      <div className="grid g-2">
        <Card title="Detector thresholds">
          {p ? (
            <dl className="kv">{Object.entries(p.detector).map(([k, v]) => <Fragment key={k}><dt>{label(k)}</dt><dd>{THRESHOLDS[k]?.(v) ?? v}</dd></Fragment>)}</dl>
          ) : <Skeleton h={120} />}
        </Card>
        <Card title="Read-only evidence tools">
          {p ? <div className="row">{p.readonly_tools.map(t => <span key={t} className="chip">{t}</span>)}</div> : <Skeleton h={60} />}
          {sys && (
            <dl className="kv" style={{ marginTop: 16 }}>
              {sys.llm_endpoints.map(e => (
                <Fragment key={e.name}><dt>Model endpoint</dt>
                  <dd><Badge tone={e.healthy ? 'good' : 'crit'}>{e.healthy ? 'Healthy' : 'Unreachable'}</Badge> <code>{e.model}</code> <span className="muted small">via {e.name}</span></dd></Fragment>
              ))}
            </dl>
          )}
        </Card>
      </div>
    </Page>
  )
}
