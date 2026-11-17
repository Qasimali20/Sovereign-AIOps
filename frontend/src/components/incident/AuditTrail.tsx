import type { AuditEvent, Bundle } from '../../lib/api'
import { Card } from '../ui'
import { clock, label } from '../../lib/format'
import { actorIcon } from './parts'

/** The one-line detail shown after each audit event (only events that have a useful one). */
const DETAIL: Record<string, (d: AuditEvent['data']) => string> = {
  condition_cleared: d => d?.evidence ?? '',
  status: d => label(d?.status ?? ''),
  policy_decision: d => label(d?.decision ?? ''),
  verification: d => label(d?.outcome ?? ''),
  diagnosis: d => (d?.meta?.latency_s != null ? `${d.meta.latency_s}s` : ''),
}

/** Every step of the incident, who or what did it, and when (from the hash-chained audit log). */
export function AuditTrail({ b }: { b: Bundle }) {
  return (
    <Card title="Audit trail">
      <ul className="timeline">
        {b.audit_events.map(a => {
          const { cls, icon } = actorIcon(a.actor)
          const extra = DETAIL[a.event]?.(a.data) ?? ''
          return (
            <li key={a.id}>
              <span className="t">{clock(a.created)}</span>
              <span className={`tdot ${cls}`}>{icon}</span>
              <span>{label(a.event)}{extra && <b className="tl-extra"> · {extra}</b>}
                {a.data?.comment ? <span className="ink2"> — “{a.data.comment}”</span> : null}
                <div className="actor">{a.actor}</div>
              </span>
            </li>
          )
        })}
      </ul>
    </Card>
  )
}
