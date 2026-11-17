/* Incident page building blocks: the stage pipeline, evidence references, the proposed-action body. */

import { Bot, Check, Clock, Gavel, Play, Server, User as UserIcon, X } from 'lucide-react'
import type { Bundle, Proposal } from '../../lib/api'

import { label } from '../../lib/format'

export const CONF = { high: 'info', medium: 'warn', low: 'neutral' } as const

export const latestStep = (b: Bundle, node: string) => [...b.agent_steps].reverse().find(s => s.node === node)

export function pipeline(b: Bundle) {
  const proposal = b.action_proposals.at(-1)
  const policy = b.policy_decisions.at(-1)
  const approval = b.approval_requests.at(-1)
  const exec = b.execution_records.at(-1)
  const ver = b.verification_results.at(-1)
  const done = b.report != null || b.incident.status === 'closed'
  const needs = policy?.decision === 'requires_approval'
  type S = 'done' | 'current' | 'failed' | 'skipped' | 'todo'
  const st: [string, S][] = [
    ['Detected', 'done'],
    ['Evidence', b.evidence_records.length > 1 ? 'done' : 'current'],
    ['Diagnosis', latestStep(b, 'diagnose') ? 'done' : 'todo'],
    ['Proposal', proposal ? 'done' : done ? 'skipped' : 'todo'],
    ['Policy', policy ? (policy.decision === 'deny' ? 'failed' : 'done') : done ? 'skipped' : 'todo'],
    ['Approval', !needs ? (done ? 'skipped' : 'todo') : approval?.status === 'approved' ? 'done' : approval?.status === 'rejected' ? 'failed' : 'current'],
    ['Execution', exec ? (exec.ok ? 'done' : 'failed') : done ? 'skipped' : 'todo'],
    ['Verification', ver ? (ver.outcome === 'resolved' ? 'done' : 'failed') : done ? 'skipped' : 'todo'],
    ['Closed', done ? 'done' : 'todo'],
  ]
  if (!done && !st.some(([, s]) => s === 'current' || s === 'failed')) {
    const i = st.findIndex(([, s]) => s === 'todo')
    if (i >= 0) st[i][1] = 'current'
  }
  return st
}

export function Pipeline({ b }: { b: Bundle }) {
  return (
    <div className="pipeline">
      {pipeline(b).map(([name, s]) => (
        <div key={name} className={`pstep ${s}`}>
          <span className="pdot">{s === 'done' ? <Check size={14} strokeWidth={3} /> : s === 'failed' ? <X size={14} strokeWidth={3} /> : s === 'current' ? <Clock size={13} /> : null}</span>
          {name}
        </div>
      ))}
    </div>
  )
}

export function Ref({ id, known, onOpen }: { id: string; known: Set<string>; onOpen: (id: string) => void }) {
  if (!known.has(id)) return <span className="chip bad" title="This id was not recorded as evidence for this incident">{id}</span>
  return <a className="chip" href={`#${id}`} onClick={e => { e.preventDefault(); onOpen(id) }}>{id}</a>
}

export function ProposalBody({ p, policy, known, onOpen }: { p: Proposal; policy?: { decision: string; reasons: string }; known: Set<string>; onOpen: (id: string) => void }) {
  let reasons: string[] = []
  try { reasons = policy ? JSON.parse(policy.reasons) : [] } catch { /* ignore */ }
  return (
    <div className="stack" style={{ gap: 16 }}>
      <dl className="kv">
        <dt>Operation</dt><dd><code>{p.operation}</code></dd>
        <dt>Target</dt><dd><code>{p.target.resource_id}</code> <span className="muted">({p.target.resource_type})</span></dd>
        <dt>Why</dt><dd>{p.reason}</dd>
        <dt>Expected</dt><dd>{p.expected_outcome}</dd>
        <dt>Rollback</dt><dd>{p.rollback_plan}</dd>
        <dt>Evidence</dt><dd className="row">{p.evidence_refs.length ? p.evidence_refs.map(r => <Ref key={r} id={r} known={known} onOpen={onOpen} />) : <span className="muted">none cited</span>}</dd>
      </dl>
      {policy && (
        <div className={`callout ${policy.decision === 'deny' ? 'crit' : policy.decision === 'requires_approval' ? 'warn' : 'info'}`}>
          <Gavel size={16} />
          <div><b>Policy engine: {label(policy.decision)}</b>
            <ul style={{ margin: '6px 0 0', paddingLeft: 18 }}>{reasons.map(r => <li key={r}>{r}</li>)}</ul>
          </div>
        </div>
      )}
    </div>
  )
}

export function actorIcon(actor: string) {
  if (actor.startsWith('human:')) return { cls: 'human', icon: <UserIcon size={11} /> }
  if (actor === 'policy_engine') return { cls: 'policy', icon: <Gavel size={11} /> }
  if (actor === 'restricted_executor' || actor === 'verifier') return { cls: 'exec', icon: <Play size={11} /> }
  if (actor.endsWith('_agent')) return { cls: 'agent', icon: <Bot size={11} /> }
  return { cls: '', icon: <Server size={11} /> }
}

