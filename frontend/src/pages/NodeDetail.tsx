import { useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { ChevronRight } from 'lucide-react'
import { useFleet, useIncidents, useNodeHistory } from '../lib/queries'
import { TimeChart } from '../components/charts'
import { Badge, Card, Empty, Kpi, PageHead, Segmented, Skeleton, StatusBadge } from '../components/ui'
import { KindTag } from '../components/ui'
import { ModelName } from '../lib/modelNames'
import { ago, label, num, pct, uptime } from '../lib/format'

type Range = '15m' | '1h' | '24h' | '7d' | '30d'

export default function NodeDetail() {
  const { node = '' } = useParams()
  const [range, setRange] = useState<Range>('1h')
  const fleet = useFleet()
  const hist = useNodeHistory(node, range)
  const incidents = useIncidents()
  const n = fleet.data?.[node]

  const sys = (hist.data?.points ?? []).map(p => ({ t: p[0], gpu: p[1], cpu: p[2], mem: p[3] }))
  // token-rate charts only where there is something to show (embedders/rerankers generate no tokens)
  const models = Object.entries(hist.data?.models ?? {}).filter(([, m]) => m.points.some(p => (p[1] ?? 0) > 0 || (p[2] ?? 0) > 0))
  const nodeIncidents = (incidents.data ?? []).filter(i => i.node === node).slice(0, 8)
  const pctFmt = (v: number) => `${v.toFixed(0)}%`

  return (
    <main className="content">
      <div className="crumbs"><Link to="/fleet">Fleet</Link><ChevronRight size={14} /><span>{node}</span></div>
      <PageHead title={<span className="row" style={{ gap: 12 }}>{node}<span className="muted mono" style={{ fontSize: 14, fontWeight: 400 }}>{n?.host} · {n?.ip}</span></span>}
        actions={<Segmented value={range} onChange={setRange} options={[{ key: '15m', label: '15 min' }, { key: '1h', label: '1 hour' }, { key: '24h', label: '24 hours' }, { key: '7d', label: '7 days' }, { key: '30d', label: '30 days' }]} />} />

      <div className="grid g-4 kpis">
        <Kpi metric="gpu" label="GPU" value={n?.gpu?.util ?? '—'} unit="%" foot={`${n?.gpu?.temp ?? '—'}°C · ${n?.gpu?.power?.toFixed(1) ?? '—'} W · ${n?.gpu?.clock_sm ?? '—'} MHz`} />
        <Kpi metric="mem" label="Memory (shared CPU and GPU)" value={n?.mem_used_gb?.toFixed(0) ?? '—'} unit={`/ ${n?.mem_total_gb?.toFixed(0) ?? '—'} GB`} foot={n?.mem_total_gb ? pct(100 * (n.mem_used_gb ?? 0) / n.mem_total_gb, 1) + ' used' : ' '} />
        <Kpi metric="cpu" label="CPU" value={n?.cpu_percent?.toFixed(0) ?? '—'} unit="%" foot={`${n?.cpu_temp_c?.toFixed(0) ?? '—'}°C`} />
        <Kpi label="Uptime" value={uptime(n?.uptime_h)} foot={`disk ${pct(n?.disk_used_pct)} used`} />
      </div>

      <Card title="Utilisation" hint={`GPU, CPU and memory, % · ${range}`}>
        {hist.data ? (sys.length ? <TimeChart data={sys} yDomain={[0, 100]} yFmt={pctFmt} height={260}
          series={[{ key: 'gpu', name: 'GPU', fmt: pctFmt, color: 'var(--m-gpu)' }, { key: 'cpu', name: 'CPU', fmt: pctFmt, color: 'var(--m-cpu)' },
                  { key: 'mem', name: 'Memory', fmt: pctFmt, color: 'var(--m-mem)' }]} />
          : <Empty>No history yet. The monitor keeps history while it runs.</Empty>)
          : hist.error ? <div className="err">{hist.error.message}</div> : <Skeleton h={260} />}
      </Card>

      <Card title="Model servers" bodyClass="card-body tight">
        {!n?.servers?.length ? <Empty>No model servers on this node.</Empty> : (
          <div className="table-wrap">
            <table className="table">
              <thead><tr><th>Model</th><th>Kind</th><th>Port</th><th>Status</th><th className="num">Running</th><th className="num">Waiting</th><th className="num">Tokens/s</th><th className="num">TTFT p95</th><th className="num">Mem share</th></tr></thead>
              <tbody>
                {n.servers.map(s => (
                  <tr key={`${s.name}@${s.port}`}>
                    <td><ModelName name={s.name} className="primary" /></td><td><KindTag kind={s.kind} /></td><td className="mono">{s.port}</td>
                    <td>{s.status === 'ready' ? <Badge tone="good">Ready</Badge> : <Badge tone="warn">{label(s.status)}</Badge>}</td>
                    <td className="num">{num(s.running, 0)}</td><td className="num">{num(s.waiting, 0)}</td>
                    <td className="num">{num(s.tokens_per_s)}</td><td className="num">{s.ttft_p95 == null ? '—' : `${s.ttft_p95.toFixed(2)}s`}</td>
                    <td className="num">{s.mem_fraction == null ? '—' : pct(100 * s.mem_fraction)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {models.length > 0 && (
        <div className="grid g-2">
          {models.map(([key, m]) => {
            const pts = m.points.map(p => ({ t: p[0], gen: p[1], prompt: p[2] }))
            return (
              <Card key={key} title={<ModelName name={m.name} inline />} hint={`${m.kind} · port ${m.port} · tokens/s · ${range}`}>
                <TimeChart data={pts} height={170} yFmt={v => v.toFixed(0)}
                  series={[{ key: 'gen', name: 'Generated', fmt: v => `${v.toFixed(1)}/s` }, { key: 'prompt', name: 'Prompt', fmt: v => `${v.toFixed(1)}/s` }]} />
              </Card>
            )
          })}
        </div>
      )}

      <Card title="Incidents on this node" bodyClass="card-body tight">
        {nodeIncidents.length === 0 ? <Empty>No incidents recorded for {node}.</Empty> : (
          <table className="table"><tbody>
            {nodeIncidents.map(i => (
              <tr key={i.id}><td><Link to={`/incidents/${i.id}`} className="primary">{i.title}</Link><div className="sub">{i.id}</div></td>
                <td><StatusBadge status={i.status} /></td><td className="muted">{ago(i.created)}</td></tr>
            ))}
          </tbody></table>
        )}
      </Card>
    </main>
  )
}
