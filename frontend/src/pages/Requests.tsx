import { useMemo, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { Activity, Zap } from 'lucide-react'
import type { LlmRange, LlmRequest } from '../lib/api'
import { useLlmEnergy, useLlmRequests, useLlmSummary } from '../lib/queries'
import { Card, Empty, Page, PageHead, Segmented, Skeleton } from '../components/ui'
import { ModelName } from '../lib/modelNames'
import { Tile } from '../components/Tile'
import { TimeChart } from '../components/charts'
import { ago, dateTime } from '../lib/format'

const RANGES: { key: LlmRange; label: string }[] = [{ key: '1h', label: '1 hour' }, { key: '6h', label: '6 hours' },
  { key: '24h', label: '24 hours' }, { key: '7d', label: '7 days' }]
const secs = (v: number | null | undefined) => v == null ? '—' : v < 1 ? `${Math.round(v * 1000)} ms` : `${v.toFixed(v < 10 ? 2 : 1)} s`
const count = (n: number) => n >= 1e6 ? `${(n / 1e6).toFixed(2)} M` : n >= 1e3 ? `${(n / 1e3).toFixed(1)} k` : String(Math.round(n))
const pct = (v: number | null | undefined) => v == null ? '—' : `${(v * 100).toFixed(v < 0.01 && v > 0 ? 2 : 1)}%`
const wh = (v: number | null | undefined) => v == null ? '—' : v >= 1000 ? `${(v / 1000).toFixed(2)} kWh` : v >= 1 ? `${v.toFixed(1)} Wh` : `${(v * 1000).toFixed(0)} mWh`
const PAGE = 25
const OUTCOME: Record<string, string> = { ok: 'OK', error: 'Upstream error', client_error: 'Rejected', cut: 'Stream cut', not_found: 'Unknown model', unavailable: 'No replica' }

/** LLMOps: every request the model gateway served (metadata only, no prompt or answer text), with the GPU energy
 *  each model used. */
export default function Requests({ embedded = false }: { embedded?: boolean }) {
  const [params, setParams] = useSearchParams()
  const range = (RANGES.find(r => r.key === params.get('range'))?.key ?? '24h') as LlmRange
  const model = params.get('model') ?? '', client = params.get('client') ?? '', outcome = params.get('outcome') ?? ''
  const [page, setPage] = useState(0)
  // a new filter starts again at the newest page
  const set = (k: string, v: string) => { setPage(0); setParams(p => { const n = new URLSearchParams(p); if (v) n.set(k, v); else n.delete(k); return n }, { replace: true }) }
  const s = useLlmSummary(range).data
  const e = useLlmEnergy(range).data
  const list = useLlmRequests(range, { model, client, outcome }).data
  const energyOf = useMemo(() => new Map((e?.models ?? []).map(m => [m.model, m])), [e])
  const series = useMemo(() => (s?.series ?? []).map(([t, n, err]) => ({ t, ok: n - err, errors: err })), [s])

  return (
    <Page embedded={embedded}>
      <PageHead embedded={embedded} title="Requests" actions={<Segmented value={range} onChange={v => set('range', v === '24h' ? '' : v)} options={RANGES} />} />
      <div className="ftiles">
        <Tile i={0} label="Requests" color="var(--m-gpu)" color2="var(--m-gpu-2)" value={s ? s.total.requests : null} fmt={count}
          foot={s ? `${s.total.clients} client${s.total.clients === 1 ? '' : 's'} · through the gateway` : undefined} />
        <Tile i={1} label="Failed" color={s?.total.errors ? 'var(--crit)' : 'var(--m-bar)'} value={s ? (s.total.error_rate ?? 0) * 100 : null}
          unit="%" fmt={v => v.toFixed(v > 0 && v < 1 ? 2 : 1)} foot={s ? `${s.total.errors} of ${s.total.requests}` : undefined} />
        <Tile i={2} label="Tokens" color="var(--s3)" value={s ? s.total.tokens : null} fmt={count} foot={s?.total.p95_s != null ? `p95 latency ${secs(s.total.p95_s)}` : undefined} />
        <Tile i={3} label="GPU energy, active" color="var(--m-mem)" color2="var(--m-mem-2)" value={e ? e.active_wh : null} fmt={wh}
          foot={e ? `${wh(e.total_wh)} incl. idle · all 4 GPUs` : undefined} />
      </div>

      <Card title="Requests over time" hint={s ? `per ${s.step >= 3600 ? `${s.step / 3600} h` : `${s.step / 60} min`}` : undefined}>
        {!s ? <Skeleton h={200} /> : series.length ? (
          <TimeChart data={series} height={200} stacked yFmt={v => count(v)} xDomain={[s.since, s.until]}
            series={[{ key: 'ok', name: 'Served', fmt: v => count(v), color: 'var(--m-gpu)' }, { key: 'errors', name: 'Failed', fmt: v => count(v), color: 'var(--crit)' }]} />
        ) : <Empty icon={<Activity size={26} />}>No requests in this range yet. The gateway records every request it serves.</Empty>}
      </Card>

      <Card title="By model" hint="successful requests; energy is an estimate, see below" bodyClass="card-body tight">
        {!s ? <div className="card-body"><Skeleton h={160} /></div> : !s.by_model.length ? <Empty>No model served a request in this range.</Empty> : (
          <div className="table-wrap">
            <table className="table">
              <thead><tr><th>Model</th><th className="num">Requests</th><th className="num">Failed</th><th className="num">p50</th><th className="num">p95</th>
                <th className="num hide-sm">First byte p95</th><th className="num hide-sm">Tokens</th><th className="num">Energy</th>
                <th className="num hide-sm">per request</th><th className="num hide-sm">per 1k tokens</th></tr></thead>
              <tbody>
                {s.by_model.map(m => {
                  const en = energyOf.get(m.model)
                  return (
                    <tr key={m.model}>
                      <td><button className="link-btn primary" onClick={() => set('model', model === m.model ? '' : m.model)} title="Show only this model's requests"><ModelName name={m.model} inline /></button>
                        <div className="sub">{en?.node ?? ''}{en?.node ? ' · ' : ''}{m.endpoints.join(', ')}</div></td>
                      <td className="num">{count(m.requests)}</td>
                      <td className={`num${m.errors ? ' crit-ink' : ' muted'}`}>{m.errors ? `${m.errors} (${pct(m.error_rate)})` : '0'}</td>
                      <td className="num nowrap">{secs(m.p50_s)}</td><td className="num nowrap">{secs(m.p95_s)}</td>
                      <td className="num nowrap hide-sm">{secs(m.ttft_p95_s)}</td>
                      <td className="num hide-sm">{m.prompt_tokens + m.completion_tokens ? count(m.prompt_tokens + m.completion_tokens) : m.audio_s ? `${count(m.audio_s)} s audio` : '—'}</td>
                      <td className="num nowrap">{wh(en?.wh)}</td>
                      <td className="num nowrap hide-sm">{wh(en?.wh_per_request)}</td>
                      <td className="num nowrap hide-sm">{wh(en?.wh_per_1k_tokens)}</td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <div className="grid g-2">
        <Card title="By client" hint="API key name" bodyClass="card-body tight">
          {!s?.by_client.length ? <Empty>No clients yet.</Empty> : (
            <table className="table">
              <thead><tr><th>Client</th><th className="num">Requests</th><th className="num">Failed</th></tr></thead>
              <tbody>{s.by_client.map(c => (
                <tr key={c.client}><td><button className="link-btn primary" onClick={() => set('client', client === c.client ? '' : c.client)}>{c.client}</button></td>
                  <td className="num">{count(c.requests)}</td><td className={`num${c.errors ? ' crit-ink' : ' muted'}`}>{c.errors}</td></tr>))}</tbody>
            </table>
          )}
        </Card>
        <Card title="GPU energy by node" hint={e ? `average draw over ${e.hours >= 1 ? `${+e.hours.toFixed(1)} h` : `${Math.round(e.hours * 60)} min`}` : undefined} bodyClass="card-body tight">
          {!e ? <div className="card-body"><Skeleton h={120} /></div> : !e.nodes.length ? <Empty icon={<Zap size={22} />}>Prometheus did not answer.</Empty> : (
            <table className="table">
              <thead><tr><th>Node</th><th className="num">Average</th><th className="num">Idle</th><th className="num">Active energy</th></tr></thead>
              <tbody>{e.nodes.map(n => (
                <tr key={n.node}><td><Link to={`/fleet/${n.node}`}>{n.node}</Link></td><td className="num">{n.avg_w} W</td>
                  <td className="num muted">{n.idle_w} W</td><td className="num">{wh(n.active_wh)}</td></tr>))}</tbody>
            </table>
          )}
          {e && <p className="small muted" style={{ margin: '10px 18px 14px' }}>
            Covers {e.since > (s?.since ?? 0) + 60 ? `since ${dateTime(e.since)}, when request recording began` : 'the whole range'}. How it is estimated: {e.method}.</p>}
        </Card>
      </div>

        <Card title="Recent requests" hint="metadata only: no prompt or answer text is stored"
          right={<Segmented value={outcome || 'all'} onChange={v => set('outcome', v === 'all' ? '' : v)}
            options={[{ key: 'all', label: 'All' }, { key: 'errors', label: 'Failed' }]} />} bodyClass="card-body tight">
          {(model || client) && <div className="filter-note">Showing {[model && `model ${model}`, client && `client ${client}`].filter(Boolean).join(' and ')}
            {' · '}<button className="link-btn" onClick={() => { setPage(0); setParams(p => { const n = new URLSearchParams(p); n.delete('model'); n.delete('client'); return n }, { replace: true }) }}>show all</button></div>}
          {!list ? <div className="card-body"><Skeleton h={200} /></div> : !list.length ? <Empty>No requests match.</Empty> : (
            <div className="table-wrap">
              <table className="table">
                <thead><tr><th>When</th><th>Model</th><th className="hide-sm">Client</th><th>Outcome</th><th className="num">Latency</th>
                  <th className="num hide-sm">First byte</th><th className="num hide-sm">Tokens in / out</th></tr></thead>
                <tbody>{list.slice(page * PAGE, (page + 1) * PAGE).map((r: LlmRequest) => (
                  <tr key={r.id}>
                    <td className="muted nowrap" title={`${dateTime(r.t)} · ${r.id}`}>{ago(r.t)}</td>
                    <td><ModelName name={r.model ?? r.requested} inline className="primary" /><div className="sub">{r.node ?? '—'} · {r.endpoint}{r.model && r.requested !== r.model ? ` · asked for ${r.requested}` : ''}{(r.attempts ?? 1) > 1 ? ' · failed over' : ''}</div></td>
                    <td className="hide-sm">{r.client}</td>
                    <td className="nowrap"><span className={`outcome o-${r.outcome}`}>{OUTCOME[r.outcome] ?? r.outcome}</span>{r.status && r.outcome !== 'ok' ? <span className="muted small"> {r.status}</span> : null}</td>
                    <td className="num nowrap">{secs(r.latency_s)}</td>
                    <td className="num nowrap hide-sm">{secs(r.ttft_s)}</td>
                    <td className="num nowrap hide-sm">{r.prompt_tokens || r.completion_tokens ? `${count(r.prompt_tokens ?? 0)} / ${count(r.completion_tokens ?? 0)}` : r.audio_s ? `${r.audio_s.toFixed(1)} s audio` : '—'}</td>
                  </tr>))}</tbody>
              </table>
            </div>
          )}
          {list && list.length > PAGE && (
            <div className="pager">
              <span>{page * PAGE + 1}–{Math.min(list.length, (page + 1) * PAGE)} of the newest {list.length}</span>
              <span className="spacer" />
              <button className="btn btn-sm" disabled={page === 0} onClick={() => setPage(n => n - 1)}>Newer</button>
              <button className="btn btn-sm" disabled={(page + 1) * PAGE >= list.length} onClick={() => setPage(n => n + 1)}>Older</button>
            </div>
          )}
        </Card>
    </Page>
  )
}
