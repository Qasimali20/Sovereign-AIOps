import { useState } from 'react'
import { Link } from 'react-router-dom'
import { CheckCircle2, ClipboardCheck, ExternalLink } from 'lucide-react'
import { canApprove, type User } from '../lib/api'
import { useApprovals } from '../lib/queries'
import { Badge, Card, Empty, PageHead, Segmented } from '../components/ui'
import { ago, dateTime, duration } from '../lib/format'

export default function Approvals({ user }: { user: User }) {
  const [status, setStatus] = useState<'pending' | 'approved' | 'rejected' | 'expired'>('pending')
  const { data } = useApprovals(status)

  return (
    <main className="content">
      <PageHead title="Approvals"
        actions={<Segmented value={status} onChange={setStatus} options={[{ key: 'pending', label: 'Pending' }, { key: 'approved', label: 'Approved' }, { key: 'rejected', label: 'Rejected' }, { key: 'expired', label: 'Expired' }]} />} />
      {!canApprove(user) && status === 'pending' && (
        <div className="callout info"><ClipboardCheck size={16} />You have viewer access. You can review proposals, but only approvers can decide.</div>
      )}
      {data === undefined ? <Empty>Loading…</Empty> : data.length === 0 ? (
        <Card><Empty icon={<CheckCircle2 size={28} />}>{status === 'pending' ? 'No actions are waiting for approval.' : `No ${status} actions yet.`}</Empty></Card>
      ) : (
        <div className="grid g-2">
          {data.map(a => (
            <Card key={a.id} className={status === 'pending' ? 'approval-card' : ''}
              title={<span style={{ fontWeight: 600 }}>{a.proposal?.operation ?? 'unknown operation'} → <code>{a.proposal?.target.resource_id}</code></span>}
              right={status === 'pending' ? <Badge tone="warn">waiting {ago(a.created).replace(' ago', '')}</Badge>
                : <Badge tone={status === 'approved' ? 'good' : 'neutral'}>{status} by {a.approver}</Badge>}>
              <div className="stack" style={{ gap: 14 }}>
                <div className="small muted">
                  <Link to={`/incidents/${a.incident_id}`}>{a.incident?.title ?? a.incident_id}</Link> · {a.incident?.node} · requested {dateTime(a.created)}
                </div>
                <dl className="kv">
                  <dt>Why</dt><dd>{a.proposal?.reason}</dd>
                  <dt>Expected</dt><dd>{a.proposal?.expected_outcome}</dd>
                  <dt>Rollback</dt><dd>{a.proposal?.rollback_plan}</dd>
                  <dt>Evidence</dt><dd>{a.proposal?.evidence_refs.length ?? 0} records cited</dd>
                  {a.decided && <><dt>Decided</dt><dd>{dateTime(a.decided)} · after {duration(a.decided - a.created)}{a.comment ? ` · “${a.comment}”` : ''}</dd></>}
                </dl>
                <div className="row">
                  <Link className={`btn ${status === 'pending' ? 'btn-primary' : ''}`} to={`/incidents/${a.incident_id}`}>
                    <ExternalLink size={15} />{status === 'pending' ? 'Review evidence & decide' : 'Open incident'}
                  </Link>
                </div>
              </div>
            </Card>
          ))}
        </div>
      )}
    </main>
  )
}
