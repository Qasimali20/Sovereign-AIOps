import { useDeferredValue, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { Download, ScrollText } from 'lucide-react'
import { useAudit, useAuditIntegrity } from '../lib/queries'
import { Badge, Card, Empty, PageHead , Page } from '../components/ui'
import { ago, dateTime, label } from '../lib/format'

const ACTOR_GROUPS = [
  { key: '', label: 'All actors' },
  { key: 'human:', label: 'People' },
  { key: 'agent', label: 'Agents' },
  { key: 'policy_engine', label: 'Policy engine' },
  { key: 'restricted_executor', label: 'Executor' },
  { key: 'verifier', label: 'Verifier' },
  { key: 'detector', label: 'Detector' },
]

const summarize = (d: unknown) => {
  if (d == null) return ''
  const s = JSON.stringify(d)
  return s.length > 140 ? `${s.slice(0, 140)}…` : s
}

export default function Audit({ embedded = false }: { embedded?: boolean }) {
  const [actor, setActor] = useState('')
  const [event, setEvent] = useState('')
  const [q, setQ] = useState('')
  const dq = useDeferredValue(q.toLowerCase())
  const { data } = useAudit({ limit: 1000, actor, event })
  const { data: integrity } = useAuditIntegrity()

  const events = useMemo(() => [...new Set((data ?? []).map(e => e.event))].sort(), [data])
  const rows = useMemo(
    () => (data ?? []).filter(e => !dq || `${e.incident_id ?? ''} ${e.actor} ${e.event} ${JSON.stringify(e.data)}`.toLowerCase().includes(dq)),
    [data, dq],
  )

  const exportCsv = () => {
    const esc = (v: unknown) => `"${String(v ?? '').replace(/"/g, '""')}"`
    const csv = ['time,incident,actor,event,data', ...rows.map(e =>
      [new Date(e.created * 1000).toISOString(), e.incident_id, e.actor, e.event, JSON.stringify(e.data)].map(esc).join(','))].join('\n')
    const a = Object.assign(document.createElement('a'), {
      href: URL.createObjectURL(new Blob([csv], { type: 'text/csv' })),
      download: `aiops-audit-${new Date().toISOString().slice(0, 19)}.csv`,
    })
    a.click()
    URL.revokeObjectURL(a.href)
  }

  return (
    <Page embedded={embedded}>
      <PageHead embedded={embedded} title="Audit log"
        actions={<button className="btn" onClick={exportCsv} disabled={!rows.length}><Download size={15} />Export CSV</button>} />
      {integrity && integrity.ok !== null && (
        <div className="row small" style={{ gap: 10 }}>
          {integrity.ok
            ? <Badge tone="good">Integrity verified</Badge>
            : <Badge tone="crit">Integrity check failed</Badge>}
          <span className="ink2">
            {integrity.ok
              ? `${integrity.events} events in an unbroken hash chain; edits and deletions are blocked and would be detected.`
              : `The hash chain breaks at event #${integrity.first_bad_seq} (${integrity.reason}). Restore from backup and investigate.`}
          </span>
          {integrity.checked && <span className="muted">checked {ago(integrity.checked)}</span>}
        </div>
      )}
      <Card bodyClass="card-body tight">
        <div className="filters">
          <input className="search-in" placeholder="Search incident, actor, details…" value={q} onChange={e => setQ(e.target.value)} />
          <select value={actor} onChange={e => setActor(e.target.value)}>
            {ACTOR_GROUPS.map(g => <option key={g.key} value={g.key}>{g.label}</option>)}
          </select>
          <select value={event} onChange={e => setEvent(e.target.value)}>
            <option value="">All events</option>{events.map(ev => <option key={ev} value={ev}>{label(ev)}</option>)}
          </select>
          <span className="muted small">{rows.length} events</span>
        </div>
        {data === undefined ? <Empty>Loading…</Empty> : rows.length === 0 ? <Empty icon={<ScrollText size={26} />}>No events match.</Empty> : (
          <div className="table-wrap">
            <table className="table">
              <thead><tr><th>Time</th><th>Event</th><th>Actor</th><th>Incident</th><th className="hide-sm">Details</th></tr></thead>
              <tbody>
                {rows.map(e => (
                  <tr key={e.id}>
                    <td className="mono small nowrap">{dateTime(e.created)}</td>
                    <td className="primary nowrap">{label(e.event)}</td>
                    <td className="nowrap">{e.actor.startsWith('human:') ? <b style={{ fontWeight: 500 }}>{e.actor.slice(6)}</b> : <span className="ink2">{e.actor}</span>}</td>
                    <td>{e.incident_id ? <Link className="mono small" to={`/incidents/${e.incident_id}`}>{e.incident_id}</Link> : <span className="muted">—</span>}</td>
                    <td className="mono small muted hide-sm" style={{ maxWidth: 520, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={JSON.stringify(e.data)}>{summarize(e.data)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </Page>
  )
}
