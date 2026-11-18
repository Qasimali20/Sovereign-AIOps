import { useMemo, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { Bot } from 'lucide-react'
import type { AgentRun, RagOverview } from '../lib/api'
import { useAgentOverview, useAgentRuns } from '../lib/queries'
import { Card, Empty, Page, PageHead, Segmented, Skeleton, StatusBadge } from '../components/ui'
import { Tile } from '../components/Tile'
import { ago, dateTime, duration } from '../lib/format'
import { ORIGIN_LABEL, VERDICT_LABEL } from '../lib/status'

const PAGE = 25
const pct = (v: number | null) => v == null ? '–' : `${Math.round(v * 100)}%`

/** How runbook retrieval ran, and whether the diagnoses used what it found. */
function Retrieval({ r }: { r: RagOverview }) {
  const ix = r.index
  return (
    <Card title="Runbook retrieval">
      <div className="grid g-2">
        <dl className="kv">
          <dt>Retrievals</dt><dd>{r.retrievals} <span className="muted small">· {Object.entries(r.methods).map(([m, n]) => `${n} ${m}`).join(', ')}</span></dd>
          <dt>Fallback share</dt><dd>{pct(r.fallback_share)} <span className="muted small">searched by keyword or failed instead of embedding + reranker</span></dd>
          <dt>Median time</dt><dd>{r.median_latency_s != null ? `${r.median_latency_s.toFixed(2)} s` : '–'}</dd>
          <dt>Used by the diagnosis</dt><dd>{r.citing} of {r.diagnosed} <span className="muted small">({pct(r.citation_rate)}) named a retrieved section</span></dd>
          <dt>Accuracy, used a runbook</dt><dd>{pct(r.accuracy_citing)} <span className="muted small">{r.rated_citing} rated</span></dd>
          <dt>Accuracy, did not</dt><dd>{pct(r.accuracy_not_citing)} <span className="muted small">{r.rated_not_citing} rated</span></dd>
          <dt>Evidence tools failed</dt><dd>{r.tool_errors}</dd>
          <dt>Index</dt><dd>{ix.embedded} of {ix.searchable} sections embedded · {ix.documents} documents
            {!ix.embedding_ok && <span className="muted small"> · embedding model not answering: keyword search in use</span>}</dd>
        </dl>
        <div className="table-wrap">
          <table className="table">
            <thead><tr><th>Most retrieved sections</th><th className="num">Retrieved</th><th className="num">Used</th></tr></thead>
            <tbody>{r.sections.map(s => (
              <tr key={s.id}><td className="mono small">{s.id}</td><td className="num">{s.retrieved}</td><td className="num">{s.used}</td></tr>
            ))}</tbody>
          </table>
        </div>
      </div>
    </Card>
  )
}

const tokens = (n: number) => n >= 1e6 ? `${(n / 1e6).toFixed(2)} M` : n >= 1e3 ? `${(n / 1e3).toFixed(1)} k` : String(n)

/** AgentOps: every investigation the incident agent ran, newest first, with its model use and the human verdict. */
export default function AgentRuns({ embedded = false }: { embedded?: boolean }) {
  const nav = useNavigate()
  const { data } = useAgentRuns()
  const o = useAgentOverview().data
  const [show, setShow] = useState<'all' | 'unrated' | 'rated'>('all')
  const [page, setPage] = useState(0)
  const rows = useMemo(() => (data ?? []).filter(r => show === 'all' || (show === 'rated' ? r.feedback : !r.feedback)), [data, show])
  const pages = Math.ceil(rows.length / PAGE)
  const shown = rows.slice(page * PAGE, (page + 1) * PAGE)

  return (
    <Page embedded={embedded}>
      <PageHead embedded={embedded} title="Agent runs" />
      <div className="ftiles">
        <Tile i={0} label="Runs, last 30 days" color="var(--m-gpu)" color2="var(--m-gpu-2)" value={o ? o.runs : null}
          foot={o?.median_run_s != null ? `median ${duration(o.median_run_s)} per run` : undefined} />
        <Tile i={1} label="Model calls" color="var(--s3)" value={o ? o.model_calls : null}
          foot={o ? `${o.retries} retried · ${o.fallbacks} fell back to another model` : undefined} />
        <Tile i={2} label="Tokens used" color="var(--m-mem)" color2="var(--m-mem-2)" value={o ? o.tokens : null} fmt={tokens}
          foot="prompt + answer, all model calls" />
        <Tile i={3} label="Diagnosis accuracy" color="var(--good)" color2="var(--good-2)"
          value={o?.accuracy != null ? o.accuracy * 100 : null} unit={o?.accuracy != null ? '%' : undefined}
          pct={o?.accuracy != null ? o.accuracy * 100 : undefined}
          foot={o ? (o.rated ? `${o.rated} rated · ${o.verdicts.correct} correct, ${o.verdicts.partly} partly, ${o.verdicts.wrong} wrong`
            : 'no diagnosis rated yet: rate one on its run page') : undefined} />
      </div>

      {o?.rag && <Retrieval r={o.rag} />}

      <Card title="Runs" right={<Segmented value={show} onChange={k => { setShow(k); setPage(0) }}
        options={[{ key: 'all', label: 'All' }, { key: 'unrated', label: 'Not rated' }, { key: 'rated', label: 'Rated' }]} />}
        bodyClass="card-body tight">
        {data === undefined ? <div className="card-body"><Skeleton h={200} /></div> : rows.length === 0 ? (
          <Empty icon={<Bot size={28} />}>{show === 'rated' ? 'No run has been rated yet.' : 'The agent has not run yet.'}</Empty>
        ) : (
          <div className="table-wrap">
            <table className="table click">
              <thead>
                <tr><th>Incident</th><th>Outcome</th><th className="num">Run time</th><th className="num hide-sm">Model calls</th>
                  <th className="num hide-sm">Model time</th><th className="num hide-sm">Tokens</th><th className="num hide-sm" title="retrieved runbook sections the diagnosis named">Runbooks used</th><th>Diagnosis</th><th className="hide-sm">Finished</th></tr>
              </thead>
              <tbody>
                {shown.map((r: AgentRun) => (
                  <tr key={r.incident_id} onClick={() => nav(`/agent/${r.incident_id}`)}>
                    <td><Link className="primary row-link" to={`/agent/${r.incident_id}`} onClick={e => e.stopPropagation()}>{r.title}</Link>
                      <div className="sub">{r.incident_id} · {r.node}
                        {r.origin && r.origin !== 'real' && <span className={`chip chip-origin ${r.origin}`}>{ORIGIN_LABEL[r.origin]}</span>}</div></td>
                    <td className="nowrap"><StatusBadge status={r.status} /></td>
                    <td className="num nowrap">{duration(r.duration_s)}</td>
                    <td className="num hide-sm">{r.model_calls}{r.attempts > r.model_calls && <span className="muted small"> (+{r.attempts - r.model_calls} retry)</span>}</td>
                    <td className="num nowrap hide-sm">{duration(r.model_seconds)}</td>
                    <td className="num nowrap hide-sm">{tokens(r.prompt_tokens + r.completion_tokens)}</td>
                    <td className="num nowrap hide-sm">{r.rag_cited == null ? <span className="muted">–</span>
                      : `${r.rag_cited.filter(i => r.rag_retrieved?.includes(i)).length} of ${r.rag_sections ?? 0}`}</td>
                    <td className="nowrap">{r.feedback ? <span className={`verdict v-${r.feedback}`}>{VERDICT_LABEL[r.feedback]}</span> : <span className="muted">Not rated</span>}</td>
                    <td className="muted nowrap hide-sm" title={dateTime(r.ended)}>{ago(r.ended)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {rows.length > PAGE && (
          <div className="pager">
            <span>{page * PAGE + 1}–{Math.min(rows.length, (page + 1) * PAGE)} of {rows.length}</span>
            <span className="spacer" />
            <button className="btn btn-sm" disabled={page === 0} onClick={() => setPage(p => p - 1)}>Previous</button>
            <button className="btn btn-sm" disabled={page >= pages - 1} onClick={() => setPage(p => p + 1)}>Next</button>
          </div>
        )}
      </Card>
    </Page>
  )
}
