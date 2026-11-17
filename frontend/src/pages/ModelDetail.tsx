import { useNavigate, useParams, Link, useSearchParams } from 'react-router-dom'
import { useModelBoard } from '../lib/queries'
import { ModelName, useModelNames } from '../lib/modelNames'
import type { ScoreRow } from '../lib/api'
import { Card, Empty, KindTag, Kpi, PageHead, Segmented, Skeleton, StatusBadge } from '../components/ui'
import { Panel, RANGES, fmt, type Range } from '../components/MetricPanel'
import { ago, seconds as ms } from '../lib/format'


function Summary({ r, kind }: { r: ScoreRow | null; kind: string }) {
  const gen = ['LLM', 'VLM', 'OCR'].includes(kind)
  if (!r) return null
  return (
    <div className="grid g-4 kpis">
      <Kpi label="Requests" value={r.requests == null ? '—' : Math.round(r.requests).toLocaleString()} foot={r.rpm != null ? `${fmt('rpm', r.rpm)}` : 'in range'} />
      <Kpi label="Failed" value={r.failed_pct == null ? '—' : `${r.failed_pct.toFixed(r.failed_pct < 10 ? 1 : 0)}%`} foot="aborted or errored" />
      {gen && <Kpi label="First token" value={ms(r.ttft_p95)} foot={`p95 · p99 ${ms(r.ttft_p99)}`} />}
      <Kpi label="Whole request" value={ms(r.e2e_p95)} foot={`p95 · p99 ${ms(r.e2e_p99)}`} />
      {gen && <Kpi label="KV cache" value={r.kv_pct == null ? '—' : `${r.kv_pct.toFixed(0)}%`} foot="now" />}
      <Kpi label="Running / waiting" value={r.running == null ? '—' : `${r.running} / ${r.waiting ?? 0}`} foot="now" />
    </div>
  )
}

export default function ModelDetail() {
  const { model = '' } = useParams()
  const [sp, setSp] = useSearchParams()
  const nav = useNavigate()
  const range = (sp.get('range') as Range) || '6h'
  const { data, error } = useModelBoard(model, range)
  const fullName = useModelNames()
  const sections = data ? [...new Set(data.panels.map(p => p.section))] : []
  const span: [number, number] = data ? [data.start, data.end] : [0, 0]

  return (
    <main className="content">
      <PageHead
        title={<span className="row" style={{ gap: 10 }}>{data && <KindTag kind={data.model.kind} />}<ModelName name={data?.model.name ?? model} inline /></span>}
        actions={
          <div className="row wrap" style={{ gap: 8 }}>
            <select className="input" value={data?.model.name ?? model} aria-label="Model"
              onChange={e => nav(`/models/${encodeURIComponent(e.target.value)}?range=${range}`)}>
              {(data?.models ?? []).map(m => <option key={m.id + m.node} value={m.name}>{fullName(m.name)} · {m.node}</option>)}
            </select>
            <Segmented options={RANGES} value={range} onChange={r => setSp({ range: r }, { replace: true })} />
          </div>
        } />
      {data && <div className="crumbs small ink2" style={{ marginTop: -8, marginBottom: 14 }}>
        <Link to="/models">Models</Link> · <Link to={`/fleet/${data.model.node}`}>{data.model.node}</Link> · <span className="mono">{data.model.id}</span></div>}

      {error ? <Empty>{(error as Error).message}</Empty> : !data ? <Skeleton h={300} /> : (
        <>
          <Summary r={data.summary} kind={data.model.kind} />
          {sections.map(sec => (
            <section key={sec} className="stack" style={{ gap: 12, marginTop: 18 }}>
              <h2 className="section-title" style={{ margin: '8px 0 0', fontSize: 15 }}>{sec}</h2>
              <div className="grid g-2">
                {data.panels.filter(p => p.section === sec).map(p => <Panel key={p.id} p={p} range={range} span={span} />)}
              </div>
            </section>
          ))}
          <section className="stack" style={{ gap: 12, marginTop: 18 }}>
            <h2 className="section-title" style={{ margin: '8px 0 0', fontSize: 15 }}>Incidents</h2>
            <Card bodyClass="card-body tight">
              {data.incidents.length ? (
                <table className="table">
                  <tbody>{data.incidents.map(i => (
                    <tr key={i.id}>
                      <td><Link className="primary" to={`/incidents/${i.id}`}>{i.title}</Link><div className="sub mono">{i.id}</div></td>
                      <td><StatusBadge status={i.status} /></td>
                      <td className="muted small nowrap">{ago(i.created)}</td>
                    </tr>))}</tbody>
                </table>
              ) : <Empty>No incidents for this model.</Empty>}
            </Card>
          </section>
        </>
      )}
    </main>
  )
}
