/* Detailed governance views (autonomy pipeline, guardrails, verification record), shown on the
 * Policies page. The Overview links here instead of repeating them. */
import { Fragment, type ReactNode } from 'react'
import { ArrowRight, CheckCircle2, FlaskConical, Lock, XCircle } from 'lucide-react'
import type { Overview } from '../lib/api'
import { Card } from './ui'
import { ago } from '../lib/format'

/* ------------------------------------------------------------------ autonomy pipeline */
function Stage({ label, value, sub, tone }: { label: string; value: number; sub?: ReactNode; tone?: 'final' | 'human' }) {
  return (
    <div className={`stage${tone ? ` stage-${tone}` : ''}`}>
      <div className="stage-top">{label}</div>
      <div className="stage-val">{value}</div>
      <div className="stage-sub">{sub ?? ' '}</div>
    </div>
  )
}

export function Pipeline({ p }: { p: Overview['pipeline'] }) {
  const stages: ReactNode[] = [
    <Stage key="d" label="Detected" value={p.detected} />,
    <Stage key="g" label="Diagnosed" value={p.diagnosed} />,
    <Stage key="p" label="Fix proposed" value={p.proposed} />,
    <Stage key="c" label="Policy check" value={p.policy.requires_approval + p.policy.allow + p.policy.deny}
      sub={<>{p.policy.requires_approval} need approval · {p.policy.allow} no-op · <b className={p.policy.deny ? 'crit' : ''}>{p.policy.deny} denied</b></>} />,
    <Stage key="h" label="Human decision" value={p.approval.approved + p.approval.rejected} tone="human"
      sub={<>{p.approval.approved} approved · {p.approval.rejected} rejected{p.approval.pending ? <> · <b className="warn">{p.approval.pending} waiting</b></> : null}</>} />,
    <Stage key="e" label="Executed" value={p.executed} />,
    <Stage key="v" label="Verified recovered" value={p.verified.resolved} tone="final"
      sub={p.verified.not_resolved ? <b className="crit">{p.verified.not_resolved} not recovered</b> : null} />,
  ]
  return (
    <div className="pipeline-flow">
      {stages.map((s, i) => <Fragment key={i}>{i > 0 && <ArrowRight size={16} className="flow-arrow" />}{s}</Fragment>)}
    </div>
  )
}

/* ------------------------------------------------------------------ cards */
export function Guardrails({ o }: { o: Overview }) {
  const g = o.guardrails
  const zero = g.executions_without_human_approval === 0
  return (
    <Card title="Safety record">
      <div className="stack" style={{ gap: 18 }}>
        <div className={`assurance ${zero ? 'ok' : 'bad'}`}>
          {zero ? <Lock size={20} /> : <XCircle size={20} />}
          <div>
            <div className="assurance-val">{g.executions_without_human_approval} of {g.executions_total}</div>
            <div className="assurance-lbl">executions without approval · all time</div>
          </div>
        </div>
        <div className="mini-stats three">
          <div><b>{g.policy_denials}</b><span>proposals denied by policy</span></div>
          <div><b>{g.refused_tool_requests}</b><span>out-of-scope tool requests refused</span></div>
          <div><b>{g.executor_refusals}</b><span>executor re-check refusals</span></div>
        </div>
        <div>
          <h3 style={{ marginBottom: 10 }}>Verification record</h3>
          <ul className="evals">
            {o.evaluations.map(e => (
              <li key={e.name}>
                {e.run ? (e.run.ok ? <CheckCircle2 size={17} className="ok-ic" /> : <XCircle size={17} className="bad-ic" />) : <FlaskConical size={17} className="muted" />}
                <div className="eval-main">
                  <div className="eval-name">{e.name.replace(/-/g, ' ')}</div>
                  <div className="eval-desc">{e.description}</div>
                </div>
                <div className="eval-res">
                  {e.run ? <><b>{e.run.passed}/{e.run.total}</b><span>{ago(e.run.t)}</span></> : <span className="muted">not run yet</span>}
                  {e.run?.meta?.model_resistance ? <span>model resisted {String(e.run.meta.model_resistance)}</span> : null}
                </div>
              </li>
            ))}
          </ul>
        </div>
      </div>
    </Card>
  )
}
