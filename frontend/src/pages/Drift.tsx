import { useState } from 'react'
import { CheckCircle2, GitCompare, RefreshCw } from 'lucide-react'
import type { DriftChange, User } from '../lib/api'
import { useDrift, useDriftAccept, useDriftCheck } from '../lib/queries'
import { Badge, Card, Empty, Modal, Page, PageHead, Skeleton, useToast } from '../components/ui'
import { ago, dateTime } from '../lib/format'

const AREA: Record<string, string> = { host: 'Host', security: 'Access', unit: 'Service unit', container: 'Container' }
const TONE = { added: 'info', removed: 'warn', changed: 'warn' } as const

function Change({ c, open, onToggle, onAccept }: { c: DriftChange; open: boolean; onToggle: () => void; onAccept?: () => void }) {
  const [area, name] = [c.key.split(':')[0], c.key.slice(c.key.indexOf(':') + 1)]
  return (
    <li>
      <div className="row" style={{ justifyContent: 'space-between', gap: 8 }}>
        <span><Badge tone={TONE[c.change]}>{c.change}</Badge> <span className="muted small">{AREA[area] ?? area}</span> <b className="mono">{name}</b></span>
        <span className="row" style={{ gap: 6 }}>
          <button className="btn btn-sm" onClick={onToggle}>{open ? 'Hide diff' : 'Show diff'}</button>
          {onAccept && <button className="btn btn-sm" onClick={onAccept}>Accept</button>}
        </span>
      </div>
      {(c.before || c.after) && <div className="small muted">{c.before ?? '—'} → {c.after ?? '—'}</div>}
      {open && <pre className="json" style={{ maxHeight: 360, whiteSpace: 'pre', overflow: 'auto' }}>{c.diff || '(no text difference)'}</pre>}
    </li>
  )
}

/** Configuration drift: what changed on each node since its accepted baseline, and versions the nodes disagree on. */
export default function Drift({ user }: { user: User }) {
  const { data } = useDrift()
  const accept = useDriftAccept(), check = useDriftCheck()
  const toast = useToast()
  const [open, setOpen] = useState<string | null>(null)
  const [pending, setPending] = useState<{ node: string; keys: string[] | null } | null>(null)
  const [reason, setReason] = useState('')
  const admin = user.role === 'admin'

  const doAccept = () => {
    if (!pending) return
    accept.mutate({ ...pending, reason }, {
      onSuccess: r => { toast('good', `${r.accepted.length} item(s) accepted as the new baseline`); setPending(null); setReason('') },
      onError: e => toast('crit', String(e)),
    })
  }

  return (
    <Page>
      <PageHead title="Configuration drift" sub={data ? `checked every ${Math.round(data.interval_s / 60)} min` : undefined}
        actions={admin && <button className="btn" disabled={check.isPending} onClick={() => check.mutate(undefined, {
          onSuccess: () => toast('good', 'configuration re-read on every node'), onError: e => toast('crit', String(e)) })}>
          <RefreshCw size={15} /> {check.isPending ? 'Checking…' : 'Check now'}</button>} />

      {data === undefined ? <Skeleton h={240} /> : (
        <>
          {Object.keys(data.inconsistent).length > 0 && (
            <Card title="Versions that differ between nodes" hint="not drift: each node is compared with its own baseline">
              <div className="table-wrap">
                <table className="table">
                  <thead><tr><th>Item</th>{Object.keys(data.nodes).map(n => <th key={n}>{n}</th>)}</tr></thead>
                  <tbody>{Object.entries(data.inconsistent).map(([k, v]) => (
                    <tr key={k}><td>{k.replace('host:', '').replace('_', ' ')}</td>
                      {Object.keys(data.nodes).map(n => <td key={n} className="mono small">{v[n] ?? '—'}</td>)}</tr>
                  ))}</tbody>
                </table>
              </div>
            </Card>
          )}

          <div className="grid g-2">
            {Object.entries(data.nodes).map(([node, d]) => (
              <Card key={node} title={node}
                hint={d.ok ? `${d.items} items · checked ${ago(d.checked ?? 0)}` : d.error ?? 'not reachable'}
                right={d.changes && d.changes.length > 0 && admin
                  ? <button className="btn btn-sm" onClick={() => setPending({ node, keys: null })}>Accept all</button> : undefined}>
                {!d.ok || d.changes == null ? <div className="muted">{d.error ?? 'not checked yet'}</div>
                  : d.changes.length === 0 ? <Empty icon={<CheckCircle2 size={22} />}>Matches its baseline</Empty> : (
                    <ul className="items">
                      {d.changes.map(c => (
                        <Change key={c.key} c={c} open={open === `${node}/${c.key}`}
                          onToggle={() => setOpen(o => o === `${node}/${c.key}` ? null : `${node}/${c.key}`)}
                          onAccept={admin ? () => setPending({ node, keys: [c.key] }) : undefined} />
                      ))}
                    </ul>
                  )}
                <div className="small muted" style={{ marginTop: 10 }}>
                  Baseline {d.baseline_at ? dateTime(d.updated ?? d.baseline_at) : '—'}
                  {(d.updated_by ?? d.baseline_by) && <> · {d.updated_by ?? d.baseline_by}</>}
                </div>
              </Card>
            ))}
          </div>
          {Object.values(data.nodes).every(d => !d.ok) && <Empty icon={<GitCompare size={28} />}>No node has been checked yet.</Empty>}
        </>
      )}

      {pending && (
        <Modal title={`Accept as the new baseline on ${pending.node}`} onClose={() => setPending(null)}
          footer={<>
            <button className="btn" onClick={() => setPending(null)}>Cancel</button>
            <button className="btn btn-primary" disabled={reason.trim().length < 3 || accept.isPending} onClick={doAccept}>Accept</button>
          </>}>
          <p>{pending.keys ? <>The current configuration of <b className="mono">{pending.keys[0]}</b> becomes the baseline.</>
            : 'Every current difference on this node becomes the baseline.'} The change is recorded in the audit log with your name and reason, and the drift incident clears.</p>
          <label className="field">Reason
            <input value={reason} onChange={e => setReason(e.target.value)} placeholder="e.g. TTS memory lowered so Qwen3.6 fits" maxLength={300} />
          </label>
        </Modal>
      )}
    </Page>
  )
}
