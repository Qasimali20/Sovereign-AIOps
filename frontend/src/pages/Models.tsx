import { useMemo, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { Boxes } from 'lucide-react'
import type { ScoreRow } from '../lib/api'
import { useFleet, useScorecard } from '../lib/queries'
import { Badge, Card, Empty, KindTag, KINDS, PageHead, Page, Segmented, TabbedPage } from '../components/ui'
import Requests from './Requests'
import Evaluations from './Evaluations'
import Registry from './Registry'
import { differs, useModelNames } from '../lib/modelNames'
import type { User } from '../lib/api'
import { Tile } from '../components/Tile'
import { ENGINE, ago, count as n0, label, percent as pctv, seconds as ms, severity as lvl } from '../lib/format'

type Range = '1h' | '6h' | '24h' | '7d'
const RANGES: { key: Range; label: string }[] = [{ key: '1h', label: '1 h' }, { key: '6h', label: '6 h' }, { key: '24h', label: '24 h' }, { key: '7d', label: '7 d' }]
const GEN_KINDS = ['LLM', 'VLM', 'OCR']
const SPEECH = ['ASR', 'TTS']
const rtf = (v: number | null | undefined) => v == null ? '—' : `${v.toFixed(v < 0.1 ? 3 : 2)}×`

const short = (v: number | null | undefined) => v != null && v <= 0.3 ? '< 300 ms' : ms(v)
const display = (name: string) => name.split('/').pop() ?? name

/** p95 on top, p99 underneath in the same cell. */
function Pair({ a, b }: { a: string; b: string }) {
  return <span className="pair"><span>{a}</span><small>{b}</small></span>
}

type Row = {
  key: string; name: string; full: string; label: string; kind: string; node: string; engine: string | null; mem_gb: number | null
  status: string | null; started: number | null; tokens_per_s: number | null; running: number | null; waiting: number | null
  score: ScoreRow | null
}

/** The model servers table (the Servers tab of Models). */
export function ModelServers({ embedded = false }: { embedded?: boolean }) {
  const { data } = useFleet()
  const [params, setParams] = useSearchParams()
  const node = params.get('node') ?? ''
  const [kind, setKind] = useState('')
  const [engine, setEngine] = useState('')
  const [q, setQ] = useState('')
  const [range, setRange] = useState<Range>('24h')
  const score = useScorecard(range)
  const fullName = useModelNames()

  // one row per model server: live state from the nodes, joined with traffic and quality over the range
  const all = useMemo<Row[]>(() => {
    const out = new Map<string, Row>()
    for (const [n, f] of Object.entries(data ?? {}))
      for (const s of f.servers ?? []) {
        const key = `${n}|${display(s.name)}`
        out.set(key, { key, name: display(s.name), full: s.name, label: s.full_name || display(s.name).toLowerCase(), kind: s.kind, node: n, engine: s.engine ?? null, mem_gb: s.mem_gb ?? null, status: s.status, started: s.started ?? null,
          tokens_per_s: s.tokens_per_s ?? null, running: s.running ?? null, waiting: s.waiting ?? null, score: null })
      }
    for (const r of score.data ?? []) {
      const key = `${r.node}|${r.model}`
      const row = out.get(key)
      if (row) row.score = r
      else out.set(key, { key, name: r.model, full: r.model, label: fullName(r.model), kind: r.kind, node: r.node, engine: null, mem_gb: null, status: null, started: null,
        tokens_per_s: null, running: r.running, waiting: r.waiting, score: r })
    }
    const order: Record<string, number> = { LLM: 0, VLM: 1, OCR: 2, Embed: 3, Rerank: 4, ASR: 5, TTS: 6 }
    return [...out.values()].sort((a, b) => (order[a.kind] ?? 9) - (order[b.kind] ?? 9) || (b.score?.requests ?? 0) - (a.score?.requests ?? 0) || a.label.localeCompare(b.label))
  }, [data, score.data, fullName])

  const rows = all.filter(s => (!kind || s.kind === kind) && (!engine || s.engine === engine) && (!node || s.node === node) && (!q || `${s.full} ${s.label}`.toLowerCase().includes(q.toLowerCase())))
  const engines = Object.keys(ENGINE).filter(e => all.some(s => s.engine === e))
  const nodes = [...new Set(all.map(s => s.node))].sort()
  const live = all.filter(s => s.status != null)
  const idle = live.filter(s => s.status === 'idle').length
  const totalTok = live.reduce((a, s) => a + (s.tokens_per_s ?? 0), 0)
  const busy = live.filter(s => (s.running ?? 0) > 0).length
  const ready = live.filter(s => s.status === 'ready').length
  const kinds = [...new Set(live.map(s => s.kind))]

  return (
    <Page embedded={embedded}>
      <PageHead embedded={embedded} title="Models" />
      <div className="ftiles">
        <Tile i={0} label="Model servers ready" color="var(--good)" color2="var(--good-2)" value={data ? ready : null} of={live.length - idle}
          pct={live.length - idle ? (100 * ready) / (live.length - idle) : null} foot={idle ? `${idle} not loaded` : undefined} />
        <Tile i={1} label="Serving now" color="var(--m-gpu)" color2="var(--m-gpu-2)" value={data ? busy : null} of={live.length - idle}
          pct={live.length - idle ? (100 * busy) / (live.length - idle) : null} />
        <Tile i={2} label="Generation throughput" color="var(--s3)" value={data ? totalTok : null} unit="tok/s" bump={50} foot="sum across fleet" />
        <Tile i={3} label="Kinds" color="var(--m-mem)" color2="var(--m-mem-2)" value={data ? kinds.length : null} foot={kinds.join(' · ')} />
      </div>
      <Card bodyClass="card-body tight">
        <div className="filters">
          <input className="search-in" aria-label="Filter models" placeholder="Filter by model name" value={q} onChange={e => setQ(e.target.value)} />
          <select aria-label="Node" value={node} onChange={e => setParams(e.target.value ? { node: e.target.value } : {})}>
            <option value="">All nodes</option>{nodes.map(n => <option key={n}>{n}</option>)}
          </select>
          <select value={kind} onChange={e => setKind(e.target.value)} aria-label="Kind">
            <option value="">All kinds</option>{KINDS.filter(k => all.some(s => s.kind === k)).map(k => <option key={k} value={k}>{k}</option>)}
          </select>
          <select value={engine} onChange={e => setEngine(e.target.value)} aria-label="Engine">
            <option value="">All engines</option>{engines.map(e => <option key={e} value={e}>{ENGINE[e]}</option>)}
          </select>
          <span className="spacer" />
          <Segmented value={range} onChange={setRange} options={RANGES} />
        </div>
        {!data && !score.data ? <Empty>Loading…</Empty> : rows.length === 0 ? <Empty icon={<Boxes size={26} />}>No model servers match.</Empty> : (
          <div className="table-wrap">
            <table className="table scorecard">
              <thead><tr>
                <th>Model</th>
                <th>Node</th>
                <th>Engine</th>
                <th>Status</th>
                <th className="num" title="completed requests in the range">Reqs</th>
                <th className="num" title="aborted or errored, in the range">Failed</th>
                <th className="num" title="time to the first output: first token (chat, vision, OCR), first text (speech recognition), first audio (text-to-speech). p95, with p99 below. Blank for embedding and rerank, which return one result.">First output<span className="th-sub">p95 · p99</span></th>
                <th className="num" title="text models: time per generated token (p95). Speech models: real-time factor, processing time / audio length (p50); below 1× is faster than real time.">Speed</th>
                <th className="num" title="whole request: p95, with p99 below">Request<span className="th-sub">p95 · p99</span></th>
                <th className="num" title="share of prompt tokens served from cache">Cache hits</th>
                <th className="num" title="running / waiting now">Run / wait</th>
              </tr></thead>
              <tbody>{rows.map(s => {
                const r = s.score
                const idle = !r?.requests
                const gen = GEN_KINDS.includes(s.kind)
                const speech = SPEECH.includes(s.kind)
                const single = !gen && !speech          // embedding / rerank: one result, no stream
                return (
                  <tr key={s.key} className={idle ? 'idle' : ''}>
                    <td><div className="sc-model">
                      <KindTag kind={s.kind} />
                      <Link className="primary" title={differs(s.label, s.name) ? `API name: ${s.name}` : undefined} to={`/models/${encodeURIComponent(s.name)}`}>{s.label}</Link>
                    </div></td>
                    <td className="nowrap"><Link className="ink2" to={`/fleet/${s.node}`}>{s.node}</Link></td>
                    <td className="ink2 nowrap">{s.engine ? ENGINE[s.engine] ?? s.engine : '—'}</td>
                    <td className="nowrap" title={s.started ? `up ${ago(s.started).replace(' ago', '')}` : undefined}>
                      {s.status == null ? <Badge tone="neutral">Not reporting</Badge>
                        : s.status === 'ready' ? <Badge tone="good">{s.engine === 'ollama' ? 'Loaded' : 'Ready'}</Badge>
                        : s.status === 'idle' ? <Badge tone="neutral">Not loaded</Badge>
                        : s.status === 'error' ? <Badge tone="crit">Not answering</Badge>
                        : <Badge tone="warn">{label(s.status)}</Badge>}
                      {s.engine === 'ollama' && s.mem_gb != null && <div className="small muted">{s.mem_gb} GB in memory</div>}
                      {/* KV and preemption columns were removed (almost always empty); surface them here only when they matter */}
                      {(r?.kv_pct ?? 0) >= 80 && <div className={`small ${lvl(r?.kv_pct, 80, 90)}`}>KV {pctv(r?.kv_pct)}</div>}
                      {(r?.preemptions ?? 0) > 0 && <div className={`small ${lvl(r?.preemptions, 1, 10)}`}>{n0(r?.preemptions)} preempted</div>}
                    </td>
                    <td className="num">{n0(r?.requests)}</td>
                    <td className={`num ${lvl(r?.failed_pct, 1, 5)}`}>{idle ? '—' : pctv(r?.failed_pct)}</td>
                    <td className={`num ${gen ? lvl(r?.ttft_p95, 5, 10) : ''}`}>{single ? null : idle ? '—'
                      : s.kind === 'TTS' ? (r?.ttfa_p95 == null ? <span className="muted small" title="recorded for streamed requests only">streamed only</span> : <Pair a={ms(r.ttfa_p95)} b={ms(r.ttfa_p99)} />)
                      : <Pair a={ms(r?.ttft_p95)} b={ms(r?.ttft_p99)} />}</td>
                    <td className={`num ${speech ? lvl(r?.rtf_p50, 0.8, 1) : ''}`}>{single ? null : idle ? '—'
                      : speech ? rtf(r?.rtf_p50)
                      : r?.tpot_p95 != null && r.tpot_p95 <= 0.01 ? '< 10 ms' : ms(r?.tpot_p95)}</td>
                    <td className="num">{idle ? '—' : <Pair a={short(r?.e2e_p95)} b={short(r?.e2e_p99)} />}</td>
                    <td className="num" title={r?.avg_prompt != null && r.avg_prompt < 16 ? `prompts average ${r.avg_prompt.toFixed(0)} tokens, shorter than one 16-token cache block, so nothing can be cached` : undefined}>
                      {s.kind === 'TTS' ? null : speech ? (r?.prefix_hit_pct == null ? null : pctv(r.prefix_hit_pct))
                        : idle ? '—' : r?.avg_prompt != null && r.avg_prompt < 16 ? <span className="muted">short prompts</span> : pctv(r?.prefix_hit_pct)}</td>
                    <td className="num">{s.running == null ? '—' : `${n0(s.running)} / ${n0(s.waiting)}`}</td>
                  </tr>
                )
              })}</tbody>
            </table>
          </div>
        )}
      </Card>
    </Page>
  )
}

const TABS = [{ key: 'servers', label: 'Servers' }, { key: 'requests', label: 'Requests' },
  { key: 'evals', label: 'Evaluations' }, { key: 'registry', label: 'Registry' }]

/** Models: the servers, what the gateway served, how well each model scores, and what each server is (LLMOps, one place). */
export default function Models({ user }: { user: User }) {
  const [params, setParams] = useSearchParams()
  const tab = TABS.some(t => t.key === params.get('tab')) ? params.get('tab')! : 'servers'
  return (
    <TabbedPage title="Models" tabs={TABS} current={tab} onChange={k => setParams(k === 'servers' ? {} : { tab: k }, { replace: true })}>
      {tab === 'requests' ? <Requests embedded /> : tab === 'evals' ? <Evaluations user={user} embedded />
        : tab === 'registry' ? <Registry embedded /> : <ModelServers embedded />}
    </TabbedPage>
  )
}
