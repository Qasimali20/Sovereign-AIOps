import { useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { ArrowLeft, Bot, Cpu, ExternalLink } from 'lucide-react'
import { canApprove, type User, type Verdict } from '../lib/api'
import { useAgentRun, useRateDiagnosis } from '../lib/queries'
import { Card, Empty, PageHead, Skeleton, StatusBadge, useToast } from '../components/ui'
import { ModelName } from '../lib/modelNames'
import { Tile } from '../components/Tile'
import { ago, duration } from '../lib/format'
import { ORIGIN_LABEL, VERDICT_LABEL } from '../lib/status'

const tokens = (n: number) => n >= 1e3 ? `${(n / 1e3).toFixed(1)} k` : String(n)

/** One investigation by the incident agent, step by step: what it did, how long each step took, every model call. */
export default function AgentRun({ user }: { user: User }) {
  const { id = '' } = useParams()
  const { data, isError } = useAgentRun(id)

  if (isError) return <main className="content"><PageHead title="Agent run" /><Card><Empty icon={<Bot size={28} />}>No agent run for {id}.</Empty></Card></main>
  if (!data) return <main className="content"><PageHead title="Agent run" /><Skeleton h={300} /></main>
  const { incident: i, summary: s, steps } = data
  return (
    <main className="content">
      <div className="small"><Link to="/governance?tab=agent" className="row" style={{ gap: 6, display: 'inline-flex' }}><ArrowLeft size={14} />All agent runs</Link></div>
      <PageHead title={i.title} actions={<Link className="btn" to={`/incidents/${i.id}`}><ExternalLink size={15} />Open incident</Link>} />
      <div className="row muted small" style={{ gap: 10, marginTop: -12, flexWrap: 'wrap' }}>
        <span className="mono">{i.id}</span><span>{i.node}</span><StatusBadge status={i.status} />
        {i.origin !== 'real' && <span className={`chip chip-origin ${i.origin}`}>{ORIGIN_LABEL[i.origin]}</span>}
        <span>finished {ago(s.ended)}</span>
      </div>

      <div className="ftiles">
        <Tile i={0} label="Run time" color="var(--m-gpu)" color2="var(--m-gpu-2)" value={s.duration_s} fmt={duration} foot={`${s.steps} steps`} />
        <Tile i={1} label="Model calls" color="var(--s3)" value={s.model_calls}
          foot={`${s.attempts - s.model_calls} retried · ${s.fallbacks} fell back`} />
        <Tile i={2} label="Time in the model" color="var(--m-mem)" color2="var(--m-mem-2)" value={s.model_seconds} fmt={duration}
          pct={s.duration_s ? (100 * s.model_seconds) / s.duration_s : undefined} foot="share of the run spent waiting on the model" />
        <Tile i={3} label="Tokens" color="var(--good)" value={s.prompt_tokens + s.completion_tokens} fmt={v => tokens(Math.round(v))}
          foot={`${tokens(s.prompt_tokens)} read · ${tokens(s.completion_tokens)} written`} />
      </div>

      <div className="grid g-main">
        <Card title="Steps" hint="each step is timed from when the previous one finished">
          <ol className="run-steps">
            {steps.map((st, n) => (
              <li key={n} className={`run-step s-${st.node}`}>
                <div className="rs-head">
                  <b>{st.label}</b>
                  <span className="muted small mono">{st.duration_s == null ? 'start' : `+${duration(st.duration_s)}`}</span>
                </div>
                {st.detail && <p className="rs-detail">{st.detail}</p>}
                {st.call && (
                  <div className="rs-call">
                    <Cpu size={13} />
                    <span><b>{st.call.model ? <ModelName name={st.call.model} inline /> : "—"}</b> via {st.call.endpoint}</span>
                    <span>{duration(st.call.latency_s)}</span>
                    <span>{tokens(st.call.prompt_tokens)} read · {tokens(st.call.completion_tokens)} written</span>
                    {(st.call.attempts ?? 1) > 1 && <span className="warn-ink">{st.call.attempts} attempts</span>}
                    {st.call.skipped.length > 0 && <span className="warn-ink" title={st.call.skipped.join('\n')}>{st.call.skipped.length} model(s) skipped</span>}
                    {st.call.request_id && <span className="mono muted">{st.call.request_id}</span>}
                  </div>
                )}
              </li>
            ))}
          </ol>
        </Card>
        <Feedback id={i.id} current={data.feedback} canRate={canApprove(user)} />
      </div>
    </main>
  )
}

function Feedback({ id, current, canRate }: { id: string; current: { verdict: Verdict; note: string; by: string; created: number } | null; canRate: boolean }) {
  const toast = useToast()
  const rate = useRateDiagnosis(id)
  const [note, setNote] = useState('')
  const send = (verdict: Verdict) => rate.mutate({ verdict, note }, {
    onSuccess: () => { toast('good', `Diagnosis rated: ${VERDICT_LABEL[verdict].toLowerCase()}`); setNote('') },
    onError: e => toast('crit', e.message),
  })
  return (
    <Card title="Was the diagnosis right?" hint="feeds the diagnosis accuracy figure">
      <div className="stack" style={{ gap: 12 }}>
        {current
          ? <div><span className={`verdict v-${current.verdict}`}>{VERDICT_LABEL[current.verdict]}</span>
              <div className="small muted" style={{ marginTop: 6 }}>by {current.by.replace('human:', '')} · {ago(current.created)}</div>
              {current.note && <p className="small" style={{ margin: '6px 0 0' }}>{current.note}</p>}</div>
          : <div className="small muted">Not rated yet.</div>}
        {canRate ? (
          <>
            <textarea className="input" rows={2} maxLength={500} placeholder="What was the real cause? (optional)"
              value={note} onChange={e => setNote(e.target.value)} />
            <div className="row" style={{ gap: 8, flexWrap: 'wrap' }}>
              {(['correct', 'partly', 'wrong'] as Verdict[]).map(v => (
                <button key={v} className={`btn btn-sm verdict-btn v-${v}`} disabled={rate.isPending} onClick={() => send(v)}>{VERDICT_LABEL[v]}</button>
              ))}
            </div>
            {current && <div className="small muted">A new verdict replaces the current one; both stay in the audit log.</div>}
          </>
        ) : <div className="small muted">Approvers can rate diagnoses.</div>}
      </div>
    </Card>
  )
}
