import { ORIGIN_LABEL, isFinal, outcomeTone } from '../lib/status'
import { EvidenceList } from '../components/incident/EvidenceList'
import { AuditTrail } from '../components/incident/AuditTrail'
import { CONF, Pipeline, ProposalBody, Ref, latestStep } from '../components/incident/parts'
import { Fragment, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import {
  AlertTriangle, Bot, CheckCircle2, ChevronRight, Clock, Cpu, Server, ShieldCheck, XCircle, Archive, RotateCcw } from 'lucide-react'
import { canApprove, type Hypothesis, type Observation, type User } from '../lib/api'
import { useCloseIncident, useDecide, useIncident, useRerunIncident } from '../lib/queries'
import { Badge, Card, Empty, Modal, PolicyBadge, StatusBadge, Tabs, useToast } from '../components/ui'
import { clock, dateTime, duration, label } from '../lib/format'

type Diagnosis = { summary: string; observations: Observation[]; hypotheses: Hypothesis[] }
export default function IncidentDetail({ user }: { user: User }) {
  const { id = '' } = useParams()
  const toast = useToast()
  const { data: b, error } = useIncident(id)
  const decideM = useDecide()
  const closeM = useCloseIncident()
  const rerunM = useRerunIncident()
  const [tab, setTab] = useState<'overview' | 'evidence' | 'timeline' | 'raw'>('overview')
  const [flash, setFlash] = useState<string | null>(null)
  const [comment, setComment] = useState('')
  const [confirm, setConfirm] = useState<null | 'approve' | 'reject'>(null)
  const [closing, setClosing] = useState(false)
  const [closeNote, setCloseNote] = useState('')
  const [verdict, setVerdict] = useState('')

  const openEv = (evId: string) => {
    setTab('evidence')
    setFlash(evId)
    window.setTimeout(() => document.getElementById(evId)?.scrollIntoView({ behavior: 'smooth', block: 'center' }), 60)
    window.setTimeout(() => setFlash(c => (c === evId ? null : c)), 2600)
  }

  if (error && !b) return <main className="content"><div className="callout crit"><XCircle size={16} />{error.message}</div><Link to="/incidents">← Incidents</Link></main>
  if (!b) return <main className="content"><Empty>Loading incident…</Empty></main>

  const inc = b.incident
  const known = new Set(b.evidence_records.map(e => e.id))
  const diag: Diagnosis | null = b.report?.diagnosis_summary
    ? { summary: b.report.diagnosis_summary, observations: b.report.observations, hypotheses: b.report.hypotheses }
    : latestStep(b, 'diagnose')?.data?.diagnosis ?? null
  const diagSteps = b.agent_steps.filter(s => s.node === 'diagnose')
  const meta = latestStep(b, 'diagnose')?.data?.meta
  const proposal = b.action_proposals.at(-1)?.data
  const policy = proposal ? b.policy_decisions.find(p => p.action_id === proposal.action_id) : undefined
  const approval = b.approval_requests.at(-1)
  const pending = approval?.status === 'pending' ? approval : null
  const exec = b.execution_records.at(-1)
  const ver = b.verification_results.at(-1)
  const report = b.report
  const secDiag = (report?.attack_techniques?.length ? report : latestStep(b, 'diagnose')?.data?.diagnosis) as
    { assessment?: string; attack_techniques?: { technique_id: string; name?: string; tactic?: string; confidence: 'low' | 'medium' | 'high'; rationale: string; evidence_ids: string[] }[] } | undefined
  const steps = report?.recommended_manual_steps ?? []
  const supportPct = report?.observation_evidence_support
  const canClose = canApprove(user) && !b.running && !['awaiting_approval', 'executing', 'verifying', 'closed'].includes(inc.status)
  const closedEv = b.audit_events.find(a => a.event === 'closed')
  const clearedEv = b.audit_events.find(a => a.event === 'condition_cleared')

  const decide = () => {
    if (!pending || !confirm) return
    const approve = confirm === 'approve'
    decideM.mutate({ approvalId: pending.id, incidentId: inc.id, approve, comment }, {
      onSuccess: () => {
        toast('good', approve ? 'Approved. The executor is running the action.' : 'Rejected. Recorded in the audit trail.')
        setComment(''); setConfirm(null)
      },
      onError: e => toast('crit', e.message),
    })
  }
  const close = () => closeM.mutate({ id: inc.id, note: closeNote, disposition: verdict }, {
    onSuccess: () => { toast('good', 'Incident closed'); setClosing(false); setCloseNote('') },
    onError: e => toast('crit', e.message),
  })

  return (
    <main className="content">
      <div className="inc-head">
        <div className="crumbs"><Link to="/incidents">Incidents</Link><ChevronRight size={14} /><span className="mono">{inc.id}</span></div>
        <div className="row" style={{ gap: 12 }}>
          <h1 style={{ overflowWrap: 'anywhere' }}>{inc.title}</h1>
          <StatusBadge status={inc.status} />
          <span className="spacer" />
          {b.agent_steps.length > 0 && <Link className="btn" to={`/agent/${inc.id}`}><Bot size={15} />Agent run</Link>}
          {canApprove(user) && inc.status === 'needs_human' && !b.running && !b.execution_records?.length &&
            <button className="btn" disabled={rerunM.isPending} onClick={() => rerunM.mutate(inc.id)}><RotateCcw size={15} />Re-run investigation</button>}
          {canClose && <button className="btn" onClick={() => setClosing(true)}><Archive size={15} />Close incident</button>}
        </div>
        <div className="meta-row">
          <span><AlertTriangle size={14} />{label(inc.kind)}</span>
          <span><Cpu size={14} /><Link to={`/fleet/${inc.node}`}>{inc.node}</Link></span>
          <span><Server size={14} /><code>{inc.resource}</code></span>
          <span><Clock size={14} />Opened {dateTime(inc.created)}</span>
          {isFinal(inc.status) && <span>Duration {duration(inc.updated - inc.created)}</span>}
          {(inc.occurrences ?? 1) > 1 && <span className="chip chip-recur" style={{ marginLeft: 0 }}>recurred {inc.occurrences}×</span>}
          {inc.origin && inc.origin !== 'real' && <span className={`chip chip-origin ${inc.origin}`} style={{ marginLeft: 0 }}>
            {ORIGIN_LABEL[inc.origin]}</span>}
        </div>
      </div>

      <div className="card card-body"><Pipeline b={b} /></div>

      {pending && proposal && (
        <section className="card approval-card">
          <div className="card-head">
            <ShieldCheck size={17} color="var(--warn-ink)" />
            <h2>Your decision is needed</h2>
            <span className="hint">requested {clock(pending.created)} · {pending.id}</span>
          </div>
          <div className="card-body stack" style={{ gap: 16 }}>
            <ProposalBody p={proposal} policy={policy} known={known} onOpen={openEv} />
            {canApprove(user) ? (
              <>
                <label className="field">Comment for the audit trail
                  <textarea rows={2} maxLength={500} value={comment} onChange={e => setComment(e.target.value)} placeholder="What did you check before deciding?" />
                </label>
                <div className="row">
                  <button className="btn btn-good" onClick={() => setConfirm('approve')}><CheckCircle2 size={16} />Approve &amp; execute</button>
                  <button className="btn btn-danger" onClick={() => setConfirm('reject')}><XCircle size={16} />Reject</button>
                  <span className="muted small">Recorded as <b className="ink2">{user.username}</b></span>
                </div>
              </>
            ) : <div className="callout info"><ShieldCheck size={16} />You have viewer access. An approver must make this decision.</div>}
          </div>
        </section>
      )}

      {(report || inc.status === 'closed' || inc.status === 'recovered') && (
        <div className={`callout ${outcomeTone(inc.status)}`}>
          {outcomeTone(inc.status) === 'good' ? <CheckCircle2 size={16} /> : <AlertTriangle size={16} />}
          <div>
            <b>{label(inc.status)}.</b>{' '}
            {inc.status === 'resolved' && 'Recovery was confirmed by independent health checks (two consecutive good readings).'}
            {inc.status === 'not_resolved' && 'The action ran, but the independent check did not confirm recovery. A person should investigate.'}
            {inc.status === 'diagnosed' && 'Investigation is complete. No automated action is allowed for this resource; follow the recommended steps, then close the incident.'}
            {inc.status === 'action_rejected' && `The proposed action was rejected by ${report?.approval?.approver}.`}
            {inc.status === 'action_denied' && 'The policy engine refused the proposed action.'}
            {inc.status === 'needs_human' && 'The workflow could not finish on its own (for example, no model endpoint was available).'}
            {inc.status === 'recovered' && <>The problem cleared without an automated action: the detector observed {clearedEv?.data?.evidence ?? 'the condition gone'}{clearedEv ? `, ${duration(clearedEv.data?.duration_s)} after it was detected` : ''}.</>}
            {inc.status === 'closed' && closedEv && <>Closed by {closedEv.actor.replace('human:', '')}: “{closedEv.data?.note}”</>}
          </div>
        </div>
      )}

      <Tabs value={tab} onChange={setTab} tabs={[
        { key: 'overview', label: 'Overview' },
        { key: 'evidence', label: 'Evidence', count: b.evidence_records.length },
        { key: 'timeline', label: 'Audit trail', count: b.audit_events.length },
        { key: 'raw', label: 'Raw record' },
      ]} />

      {tab === 'overview' && (
        <div className="grid g-main">
          <div className="stack" style={{ gap: 20 }}>
            <Card title="Diagnosis"
              hint={meta ? `${meta.model} · ${meta.latency_s}s · ${diagSteps.length} pass${diagSteps.length === 1 ? '' : 'es'}` : undefined}
              right={supportPct != null ? <Badge tone={supportPct === 1 ? 'good' : 'warn'}>{Math.round(supportPct * 100)}% of facts cite evidence</Badge> : undefined}>
              {!diag ? <Empty icon={<Bot size={26} />}>{b.running ? 'The diagnosis agent is investigating…' : 'No diagnosis recorded.'}</Empty> : (
                <div className="stack" style={{ gap: 18 }}>
                  <p className="summary">{diag.summary}</p>
                  <div className="stack">
                    <h3>Observations · facts seen in the evidence</h3>
                    <ul className="items">
                      {diag.observations.map((o, i) => (
                        <li key={i}>{o.fact}<div className="refs">{o.evidence_ids.map(r => <Ref key={r} id={r} known={known} onOpen={openEv} />)}</div></li>
                      ))}
                    </ul>
                  </div>
                  <div className="stack">
                    <h3>Hypotheses · possible causes</h3>
                    <ul className="items">
                      {diag.hypotheses.map((h, i) => (
                        <li key={i}>
                          <div className="row" style={{ alignItems: 'flex-start', flexWrap: 'nowrap' }}>
                            <Badge tone={CONF[h.confidence]}>{label(h.confidence)} confidence</Badge>
                          </div>
                          <span>{h.cause}</span>
                          {h.supporting_evidence_ids.length > 0 && <div className="refs">{h.supporting_evidence_ids.map(r => <Ref key={r} id={r} known={known} onOpen={openEv} />)}</div>}
                          {(h.runbook_ids?.length ?? 0) > 0 && <div className="muted small">Runbook: {h.runbook_ids!.map(d => <code key={d} className="mono">{d} </code>)}</div>}
                          {h.missing_information.length > 0 && <ul className="missing">{h.missing_information.map(m => <li key={m}>Missing: {m}</li>)}</ul>}
                        </li>
                      ))}
                    </ul>
                  </div>
                </div>
              )}
            </Card>
            {secDiag?.attack_techniques && secDiag.attack_techniques.length > 0 && (
              <Card title="MITRE ATT&CK mapping"
                right={secDiag.assessment ? <Badge tone={({ malicious: 'crit', suspicious: 'warn', benign_admin_activity: 'good' } as const)[secDiag.assessment as 'malicious'] ?? 'neutral'}>
                  Assessment: {label(secDiag.assessment)}</Badge> : undefined}>
                <ul className="items">
                  {secDiag.attack_techniques.map(t => (
                    <li key={t.technique_id}>
                      <div className="row"><span className="attack-id">{t.technique_id}</span><b>{t.name ?? ''}</b>
                        <span className="muted small">{t.tactic ?? ''}</span><span className="spacer" /><Badge tone={CONF[t.confidence]}>{label(t.confidence)} confidence</Badge></div>
                      <span className="ink2">{t.rationale}</span>
                      {t.evidence_ids.length > 0 && <div className="refs">{t.evidence_ids.map(r => <Ref key={r} id={r} known={known} onOpen={openEv} />)}</div>}
                    </li>
                  ))}
                </ul>
              </Card>
            )}
            {steps.length > 0 && (
              <Card title="Recommended manual steps">
                <ol style={{ margin: 0, paddingLeft: 20, display: 'grid', gap: 8 }}>{steps.map(s => <li key={s}>{s}</li>)}</ol>
              </Card>
            )}
          </div>

          <div className="stack" style={{ gap: 20 }}>
            {proposal && !pending && (
              <Card title="Proposed action" hint={proposal.action_id} right={policy && <PolicyBadge decision={policy.decision} />}>
                <ProposalBody p={proposal} policy={policy} known={known} onOpen={openEv} />
              </Card>
            )}
            {approval && approval.status !== 'pending' && (
              <Card title="Human decision">
                <dl className="kv">
                  <dt>Decision</dt><dd>{approval.status === 'approved' ? <Badge tone="good">Approved</Badge> : approval.status === 'expired' ? <Badge tone="neutral">Expired: the problem cleared first</Badge> : <Badge tone="neutral">Rejected</Badge>}</dd>
                  <dt>By</dt><dd>{approval.approver}</dd>
                  <dt>At</dt><dd>{dateTime(approval.decided)} <span className="muted">· waited {duration((approval.decided ?? 0) - approval.created)}</span></dd>
                  {approval.comment && <><dt>Comment</dt><dd>“{approval.comment}”</dd></>}
                </dl>
              </Card>
            )}
            {(exec || ver) && (
              <Card title="Execution & verification">
                <dl className="kv">
                  {exec && <><dt>Executor</dt><dd>{exec.ok ? <Badge tone="good">Succeeded</Badge> : <Badge tone="crit">Failed</Badge>} <span className="muted small">at {clock(exec.created)}</span></dd></>}
                  {exec?.data?.refused && <><dt>Refused</dt><dd>{exec.data.refused}</dd></>}
                  {ver ? <><dt>Verification</dt><dd>{ver.outcome === 'resolved' ? <Badge tone="good">Recovered</Badge> : <Badge tone="crit">Not recovered</Badge>} <span className="muted small">{(ver.data?.checks ?? []).length} independent checks</span></dd></>
                    : exec && <><dt>Verification</dt><dd className="muted">checking with fresh health probes…</dd></>}
                </dl>
              </Card>
            )}
            <Card title="Alert">
              <dl className="kv">
                <dt>Fingerprint</dt><dd><code>{inc.fingerprint}</code></dd>
                {Object.entries(inc.detail ?? {}).filter(([k]) => k !== 'source').slice(0, 6).map(([k, v]) => (
                  <Fragment key={k}><dt>{label(k)}</dt><dd><code style={{ whiteSpace: 'pre-wrap' }}>{typeof v === 'object' ? JSON.stringify(v) : String(v)}</code></dd></Fragment>
                ))}
              </dl>
            </Card>
            {report && report.errors.length > 0 && (
              <Card title="Workflow notes"><ul className="missing">{report.errors.map(e => <li key={e}>{e}</li>)}</ul></Card>
            )}
          </div>
        </div>
      )}

      {tab === 'evidence' && <EvidenceList b={b} flash={flash} />}

      {tab === 'timeline' && <AuditTrail b={b} />}

      {tab === 'raw' && (
        <Card title="Raw incident record">
          <pre className="json" style={{ borderTop: 'none', maxHeight: 640, borderRadius: 10 }}>{JSON.stringify(b, null, 2)}</pre>
        </Card>
      )}

      {confirm && proposal && (
        <Modal title={confirm === 'approve' ? 'Approve and execute?' : 'Reject this action?'} onClose={() => setConfirm(null)}
          footer={<>
            <button className="btn btn-ghost" onClick={() => setConfirm(null)}>Cancel</button>
            <button className={`btn ${confirm === 'approve' ? 'btn-good' : 'btn-danger'}`} disabled={decideM.isPending} onClick={decide}>
              {confirm === 'approve' ? 'Approve & execute' : 'Reject'}
            </button>
          </>}>
          <p><code>{proposal.operation}</code> on <code>{proposal.target.resource_id}</code></p>
          <p className="ink2">{confirm === 'approve'
            ? 'The restricted executor will run this immediately, then independent health checks will verify the result.'
            : 'Nothing will be executed. The incident will be closed as rejected.'}</p>
          <p className="muted small">This decision is recorded under your name ({user.username}){comment ? ' with your comment' : ''}.</p>
        </Modal>
      )}

      {closing && (
        <Modal title="Close incident" onClose={() => setClosing(false)}
          footer={<>
            <button className="btn btn-ghost" onClick={() => setClosing(false)}>Cancel</button>
            <button className="btn btn-primary" disabled={closeM.isPending || closeNote.trim().length < 3} onClick={close}>Close incident</button>
          </>}>
          <label className="field">Resolution note (required)
            <textarea rows={3} value={closeNote} onChange={e => setCloseNote(e.target.value)} placeholder="What was done, and why it is safe to close." />
          </label>
          <label className="field" style={{ marginTop: 14 }}>Was this a real problem?
            <select value={verdict} onChange={e => setVerdict(e.target.value)}>
              <option value="">Not sure / skip</option>
              <option value="true_positive">Yes, a real problem</option>
              <option value="false_positive">No, a false alarm</option>
              <option value="expected_activity">Expected activity (maintenance, a test)</option>
            </select>
            <span className="help">Your answer measures the detector's false-alarm rate.</span>
          </label>
        </Modal>
      )}
    </main>
  )
}
