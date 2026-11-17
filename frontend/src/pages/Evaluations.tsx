import { Fragment, useState } from 'react'
import { ChevronDown, ChevronRight, FlaskConical, Play } from 'lucide-react'
import { canApprove, type EvalResult, type User } from '../lib/api'
import { useEvalHistory, useEvals, useRunEvals } from '../lib/queries'
import { Card, Empty, Page, PageHead, Skeleton, useToast } from '../components/ui'
import { ModelName } from '../lib/modelNames'
import { Tile } from '../components/Tile'
import { TimeChart } from '../components/charts'
import { ago, dateTime, duration } from '../lib/format'

const SUITE: Record<string, string> = { chat: 'Chat answers', embeddings: 'Retrieval (embeddings)', rerank: 'Retrieval (reranker)',
  transcription: 'Transcription accuracy', speech: 'Speech round-trip', ocr: 'OCR accuracy' }
const METHOD: Record<string, string> = {
  chat: 'five prompts with exactly checkable answers: arithmetic, one exact word, JSON extraction, a fact, sorting',
  embeddings: 'six queries against six documents: is the matching document the nearest?',
  rerank: 'the same six queries: is the matching document ranked first?',
  transcription: 'a reference recording with a known transcript: 1 − word error rate',
  speech: 'two known sentences synthesised, then transcribed back with whisper-turbo: 1 − word error rate',
  ocr: 'a rendered invoice with known text: 1 − character error rate',
}
const pct = (v: number | null | undefined) => v == null ? '—' : `${Math.round(v * 100)}%`

/** LLMOps: quality evaluations of every model on fixed test sets, no judge model, with each model's history. */
export default function Evaluations({ user, embedded = false }: { user: User; embedded?: boolean }) {
  const toast = useToast()
  const { data } = useEvals()
  const run = useRunEvals()
  const [open, setOpen] = useState<string | null>(null)
  const rows = data?.models ?? []
  const scored = rows.filter(r => r.score != null)
  const regressed = rows.filter(r => r.regressed).length
  const errors = rows.filter(r => r.error).length
  const totalChecks = scored.reduce((a, r) => a + (r.total ?? 0), 0)
  const failedChecks = scored.reduce((a, r) => a + ((r.total ?? 0) - (r.passed ?? 0)), 0)
  const mean = scored.length ? scored.reduce((a, r) => a + (r.score ?? 0), 0) / scored.length : null
  const start = () => run.mutate(undefined, { onSuccess: () => toast('good', 'Evaluation run started: about a minute'), onError: e => toast('crit', e.message) })

  return (
    <Page embedded={embedded}>
      <PageHead embedded={embedded} title="Evaluations" />
      <div className="ftiles">
        <Tile i={0} label="Models evaluated" color="var(--m-gpu)" color2="var(--m-gpu-2)" value={data ? scored.length : null} of={rows.length || undefined}
          foot={data ? `every ${duration(data.interval_s)} and on demand` : undefined} />
        <Tile i={1} label="Average score" color="var(--good)" color2="var(--good-2)" value={mean != null ? mean * 100 : null} unit="%" pct={mean != null ? mean * 100 : undefined}
          foot="latest run, all models" />
        <Tile i={2} label="Quality drops" color={regressed ? 'var(--crit)' : 'var(--m-bar)'} value={data ? regressed : null}
          foot="well below the model's own recent level: opens an incident" />
        <Tile i={3} label="Failed checks" color={failedChecks ? 'var(--warn)' : 'var(--m-bar)'} value={data ? failedChecks : null}
          of={data ? totalChecks : undefined}
          foot={errors ? `${errors} model(s) could not be reached` : data?.last_run ? `last run ${ago(data.last_run)}` : 'not run yet'} />
      </div>

      <Card title="Latest results" hint="click a model for its cases and history" bodyClass="card-body tight"
        right={canApprove(user) && <button className="btn btn-sm" disabled={run.isPending || data?.running} onClick={start}>
          <Play size={14} />{data?.running ? 'Running…' : 'Run now'}</button>}>
        {!data ? <div className="card-body"><Skeleton h={240} /></div> : !scored.length ? (
          <Empty icon={<FlaskConical size={28} />}>No evaluation has run yet. The first runs within a few minutes of the platform starting.</Empty>
        ) : (
          <div className="table-wrap">
            <table className="table click">
              <thead><tr><th style={{ width: 28 }} /><th>Model</th><th>Test set</th><th>Score</th><th className="num hide-sm">Previous</th>
                <th className="num hide-sm">Recent level</th><th className="num">Passed</th><th className="hide-sm">Node</th><th className="hide-sm">Run</th></tr></thead>
              <tbody>
                {rows.map(r => (
                  <Fragment key={r.model}>
                    <tr onClick={() => setOpen(open === r.model ? null : r.model)} className={r.regressed ? 'row-crit' : ''}>
                      <td className="muted">{open === r.model ? <ChevronDown size={15} /> : <ChevronRight size={15} />}</td>
                      <td><ModelName name={r.model} className="primary" />{r.regressed && <div className="sub crit-ink">quality drop</div>}
                        {r.error && <div className="sub crit-ink" title={r.error}>not reachable</div>}</td>
                      <td className="ink2 nowrap">{SUITE[r.suite] ?? r.suite}</td>
                      <td><ScoreBar v={r.score} bad={!!r.regressed || !!r.error} /></td>
                      <td className="num muted hide-sm">{pct(r.previous)}</td>
                      <td className="num muted hide-sm">{pct(r.baseline)}</td>
                      <td className="num nowrap">{r.total ? `${r.passed} / ${r.total}` : '—'}</td>
                      <td className="nowrap hide-sm">{r.node ?? '—'}</td>
                      <td className="muted nowrap hide-sm" title={r.t ? dateTime(r.t) : ''}>{r.t ? ago(r.t) : '—'}</td>
                    </tr>
                    {open === r.model && <tr className="expand"><td colSpan={9}><Detail r={r} /></td></tr>}
                  </Fragment>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
      <p className="small muted" style={{ margin: 0 }}>Scored by fixed checks, not by another model. Requests go through the gateway
        as the <code>evals</code> client, so they appear on the Requests page. A model scoring at least 25 points under its recent
        level, and under 75%, opens an incident; it clears when a later run is back to normal.</p>
    </Page>
  )
}

function ScoreBar({ v, bad }: { v: number | null; bad: boolean }) {
  return (
    <div className="scorebar" title={pct(v)}>
      <div className="sb-track"><i style={{ width: `${(v ?? 0) * 100}%`, background: bad ? 'var(--crit)' : (v ?? 0) >= 0.9 ? 'var(--good)' : 'var(--warn)' }} /></div>
      <b>{pct(v)}</b>
    </div>
  )
}

function Detail({ r }: { r: EvalResult }) {
  const h = useEvalHistory(r.model).data
  const pts = (h ?? []).map(x => ({ t: x.t ?? 0, score: (x.score ?? 0) * 100 }))
  return (
    <div className="eval-detail">
      <div>
        <div className="small muted" style={{ marginBottom: 8 }}>{METHOD[r.suite]}</div>
        {r.error ? <div className="callout crit small">{r.error}</div> : (
          <table className="table compact">
            <thead><tr><th>Case</th><th>Result</th><th>Model's answer</th></tr></thead>
            <tbody>{(r.cases ?? []).map((c, i) => (
              <tr key={i}><td>{c.case}</td>
                <td className="nowrap"><span className={`outcome o-${c.ok ? 'ok' : 'error'}`}>{c.ok ? 'Pass' : 'Fail'}</span>{c.score != null && <span className="muted small"> {pct(c.score)}</span>}</td>
                <td className="mono small">{c.got || '—'}</td></tr>))}</tbody>
          </table>
        )}
      </div>
      <div>
        <div className="small muted" style={{ marginBottom: 8 }}>Score, last 30 days</div>
        {!h ? <Skeleton h={140} /> : pts.length > 1
          ? <TimeChart data={pts} height={150} yDomain={[0, 100]} yFmt={v => `${v}%`} series={[{ key: 'score', name: 'Score', fmt: v => `${Math.round(v)}%`, color: 'var(--m-gpu)' }]} />
          : <Empty>One run so far: the history fills in with each run.</Empty>}
      </div>
    </div>
  )
}
