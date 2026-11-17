import { Link } from 'react-router-dom'
import { History, Library } from 'lucide-react'
import type { ModelVersion } from '../lib/api'
import { useRegistry } from '../lib/queries'
import { Card, Empty, Page, PageHead, Skeleton } from '../components/ui'
import { ModelName } from '../lib/modelNames'
import { Tile } from '../components/Tile'
import { ago, dateTime } from '../lib/format'

const pct = (v: number | null | undefined) => v == null ? '—' : `${Math.round(v * 100)}%`
const short = (s: string | null) => !s ? '—' : s.length > 46 ? `…${s.slice(-44)}` : s
const CHANGE_LABEL: Record<string, string> = { engine_version: 'engine', root: 'model path', max_model_len: 'context length',
  digest: 'weights digest', quantization: 'quantisation' }

/** LLMOps: what each model server is (engine version, model path, context length, weights digest), when that last
 *  changed, and how its evaluation scores compare before and after. Track and compare only: nothing is deployed from here. */
export default function Registry({ embedded = false }: { embedded?: boolean }) {
  const { data } = useRegistry()
  const ms = data?.models ?? []
  const engines = new Set(ms.map(m => m.engine_version).filter(Boolean)).size
  const restarts = ms.reduce((a, m) => a + (m.restarts || 0), 0)

  return (
    <Page embedded={embedded}>
      <PageHead embedded={embedded} title="Model registry" />
      <div className="ftiles">
        <Tile i={0} label="Model servers" color="var(--m-gpu)" color2="var(--m-gpu-2)" value={data ? ms.length : null}
          foot={data ? `${new Set(ms.map(m => m.node)).size} nodes · checked every ${Math.round(data.refresh_s / 60)} min` : undefined} />
        <Tile i={1} label="Engine versions in use" color="var(--s3)" value={data ? engines : null}
          foot={engines > 1 ? 'nodes run different engine builds' : 'one engine build across the fleet'} />
        <Tile i={2} label="Identity changes" color={data?.changes.length ? 'var(--warn)' : 'var(--m-bar)'} value={data ? data.changes.length : null}
          foot="model, engine, path, context or digest · 90 days" />
        <Tile i={3} label="Restarts" color="var(--m-mem)" color2="var(--m-mem-2)" value={data ? restarts : null} foot="same identity, process restarted" />
      </div>

      <Card title="Current versions" hint="the server's own report where it answers on the LAN" bodyClass="card-body tight">
        {!data ? <div className="card-body"><Skeleton h={260} /></div> : !ms.length ? (
          <Empty icon={<Library size={28} />}>The registry has not read the fleet yet: it does every few minutes.</Empty>
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead><tr><th>Model</th><th>Node</th><th>Engine</th><th className="hide-sm">Model path or weights</th>
                <th className="num hide-sm">Context</th><th className="hide-sm">This version since</th><th className="num">Restarts</th>
                <th className="num">Eval score</th></tr></thead>
              <tbody>{ms.map((m: ModelVersion) => (
                <tr key={`${m.node}:${m.port}:${m.model}`}>
                  <td><Link className="primary" to={`/models/${encodeURIComponent(m.model)}`}><ModelName name={m.model} inline /></Link>
                    <div className="sub">{m.kind ?? ''}{m.versions && m.versions > 1 ? ` · ${m.versions} versions` : ''}</div></td>
                  <td className="nowrap">{m.node}<div className="sub">:{m.port}</div></td>
                  <td className="nowrap">{m.engine ?? '—'}<div className="sub mono" title={m.engine_version ?? 'not reachable from the console'}>{m.engine_version ? short(m.engine_version) : 'version not visible'}</div></td>
                  <td className="mono small hide-sm" title={m.root ?? ''}>{m.digest ? `${m.digest}${m.quantization ? ` · ${m.quantization}` : ''}` : short(m.root)}</td>
                  <td className="num hide-sm">{m.max_model_len ? m.max_model_len.toLocaleString() : '—'}</td>
                  <td className="muted nowrap hide-sm" title={dateTime(m.first_seen)}>{m.first_known ? `first seen ${ago(m.first_seen)}` : ago(m.first_seen)}</td>
                  <td className="num">{m.restarts || 0}</td>
                  <td className="num nowrap"><EvalCompare m={m} /></td>
                </tr>))}</tbody>
            </table>
          </div>
        )}
      </Card>

      <Card title="Identity changes" hint="from the audit trail" bodyClass="card-body tight">
        {!data ? <div className="card-body"><Skeleton h={80} /></div> : !data.changes.length ? (
          <Empty icon={<History size={24} />}>No model has changed identity since the registry started watching.</Empty>
        ) : (
          <table className="table">
            <thead><tr><th>When</th><th>Model</th><th>Node</th><th>What changed</th></tr></thead>
            <tbody>{data.changes.map(c => (
              <tr key={c.id}><td className="muted nowrap" title={dateTime(c.created)}>{ago(c.created)}</td><td><ModelName name={c.data.model} inline /></td><td>{c.data.node}:{c.data.port}</td>
                <td className="small">{Object.entries(c.data.changes as Record<string, [unknown, unknown]>).map(([k, [a, b]]) =>
                  <div key={k}><b>{CHANGE_LABEL[k] ?? k}</b> <span className="mono">{String(a ?? '—')}</span> → <span className="mono">{String(b ?? '—')}</span></div>)}</td></tr>))}</tbody>
          </table>
        )}
      </Card>
      <p className="small muted" style={{ margin: 0 }}>Track and compare only: the platform never deploys, upgrades or rolls back a production
        model. When a model's identity changes, its evaluation scores before and after the change are compared here.</p>
    </Page>
  )
}

function EvalCompare({ m }: { m: ModelVersion }) {
  const now = m.evals_since?.mean, before = m.evals_before?.mean
  if (now == null) return <span className="muted">—</span>
  if (before == null) return <span title={`${m.evals_since?.runs} run(s)`}>{pct(now)}</span>
  const d = Math.round((now - before) * 100)
  return <span title={`before this version ${pct(before)} over ${m.evals_before?.runs} run(s)`}>{pct(now)}
    <span className={`small ${d < 0 ? 'crit-ink' : 'muted'}`}> {d === 0 ? '±0' : d > 0 ? `+${d}` : d}</span></span>
}
