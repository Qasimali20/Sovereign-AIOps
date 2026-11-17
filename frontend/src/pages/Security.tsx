import { isActive } from '../lib/status'
import { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { Ban, Play, RotateCcw, ShieldAlert, UserX } from 'lucide-react'
import { canApprove, type User } from '../lib/api'
import { useResetLab, useRunLab, useSecurity } from '../lib/queries'
import { Badge, Card, Empty, Modal, PageHead, Skeleton, StatusBadge, useToast } from '../components/ui'
import { Tile } from '../components/Tile'
import { ago, clock, label } from '../lib/format'
import type { Tone } from '../lib/format'

const ASSESS: Record<string, Tone> = { malicious: 'crit', suspicious: 'warn', benign_admin_activity: 'good', inconclusive: 'neutral' }
const KIND_LABEL: Record<string, string> = {
  auth_success: 'Successful logins', auth_failure: 'Failed logins', invalid_user: 'Unknown-user attempts',
  sudo: 'sudo commands', sudo_failure: 'sudo auth failures', sudo_denied: 'sudo denied',
  file_created: 'File written', process_exec: 'Process started', net_connection: 'Outbound connection',
}

export default function Security({ user }: { user: User }) {
  const nav = useNavigate()
  const toast = useToast()
  const { data: d } = useSecurity(24)
  const run = useRunLab()
  const reset = useResetLab()
  const [confirmReset, setConfirmReset] = useState(false)
  const covered = d ? Object.values(d.coverage).filter(c => c.ok).length : 0
  const nodesTotal = (d && Object.keys(d.coverage).length) || 4
  const active = d ? d.incidents.filter(i => isActive(i.status)).length : 0

  const start = (s: string) => run.mutate(s, {
    onSuccess: r => toast('good', r.opened.length ? `Lab attack injected: ${r.opened.length} incident(s) opened, the agent is triaging` : `Lab attack injected (${r.events} events)`),
    onError: e => toast('crit', e.message),
  })

  return (
    <main className="content">
      <PageHead title="Security operations" />

      <div className="ftiles">
        <Tile i={0} label="Auth events (24 h)" color="var(--m-gpu)" color2="var(--m-gpu-2)" value={d ? d.events_total : null} fmt={v => Math.round(v).toLocaleString()}
          foot={d ? `${d.events_by_kind.auth_failure ?? 0} failed logins · ${d.events_by_kind.sudo_failure ?? 0} sudo failures${d.platform_checks ? ` · ${d.platform_checks.toLocaleString()} platform checks not counted` : ''}` : undefined} />
        <Tile i={1} label="Security incidents" color={active ? 'var(--crit)' : 'var(--m-bar)'} value={d ? active : null} foot={d ? `active · ${d.incidents.length} recorded` : undefined} />
        <Tile i={2} label="ATT&CK techniques seen" color="var(--s3)" value={d ? d.techniques.length : null} foot="confirmed by the agent" />
        <Tile i={3} label="Telemetry coverage" color={covered === nodesTotal ? 'var(--good)' : 'var(--warn)'} value={d ? covered : null} of={nodesTotal}
          pct={d ? (100 * covered) / nodesTotal : undefined} foot="nodes with auth logs read" />
      </div>

      <div className="grid g-main">
        <Card title="Security incidents" right={<Link to="/incidents?scope=all" className="btn btn-sm">All incidents</Link>} bodyClass="card-body tight">
          {!d ? <Skeleton h={160} /> : d.incidents.length === 0 ? <Empty icon={<ShieldAlert size={26} />}>No security incidents yet.</Empty> : (
            <table className="table click">
              <thead><tr><th>Incident</th><th>Assessment</th><th>Status</th></tr></thead>
              <tbody>
                {d.incidents.map(i => (
                  <tr key={i.id} onClick={() => nav(`/incidents/${i.id}`)}>
                    <td><Link className="primary row-link" to={`/incidents/${i.id}`} onClick={e => e.stopPropagation()}>{i.title}</Link><div className="sub">{i.id} · {i.node} · {ago(i.created)}</div></td>
                    <td>{i.assessment ? <Badge tone={ASSESS[i.assessment] ?? 'neutral'}>{label(i.assessment)}</Badge> : <span className="muted small">triaging…</span>}</td>
                    <td><StatusBadge status={i.status} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Card>

        <Card title="MITRE ATT&CK">
          {!d ? <Skeleton h={160} /> : d.techniques.length === 0 ? <Empty>No techniques mapped yet.</Empty> : (
            <ul className="attack-list">
              {d.techniques.map(t => (
                <li key={t.id}>
                  <span className="attack-id">{t.id}</span>
                  <div><div className="eval-name" style={{ textTransform: 'none' }}>{t.name}</div><div className="eval-desc">{t.tactic}</div></div>
                  <div className="eval-res"><b>{t.incidents}</b><span>{t.high} high conf.</span></div>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>

      <div className="grid g-main">
        <Card title="Top sources" hint="last 24 h, by failed logins" bodyClass="card-body tight">
          {!d ? <Skeleton h={140} /> : d.top_sources.length === 0 ? <Empty>No remote sources.</Empty> : (
            <table className="table">
              <thead><tr><th>Source</th><th>Class</th><th className="num">Failed</th><th className="num">Succeeded</th><th className="hide-sm">Nodes</th><th className="hide-sm">Last</th></tr></thead>
              <tbody>
                {d.top_sources.map(s => (
                  <tr key={s.src_ip}>
                    <td className="mono">{s.src_ip}{d.lab.blocked_ips[s.src_ip] && <> <Badge tone="good">blocked in lab</Badge></>}</td>
                    <td className="ink2 small">{s.classification}</td>
                    <td className="num">{s.failures}</td><td className="num">{s.successes}</td>
                    <td className="hide-sm">{s.nodes.map(n => <span key={n} className="chip" style={{ marginRight: 4 }}>{n}</span>)}</td>
                    <td className="muted small hide-sm nowrap">{ago(s.last)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Card>

        <Card title="Isolated lab" hint={d ? d.lab.host : ''}
          right={user.role === 'admin' ? <button className="btn btn-sm btn-ghost" onClick={() => setConfirmReset(true)}><RotateCcw size={13} />Reset</button> : undefined}>
          {!d ? <Skeleton h={160} /> : (
            <div className="stack" style={{ gap: 14 }}>
              <ul className="lab-list">
                {Object.entries(d.lab.scenarios).map(([k, v]) => (
                  <li key={k}>
                    <div><div className="eval-name">{label(k)}</div><div className="eval-desc">{v}</div></div>
                    {canApprove(user) && <button className="btn btn-sm" disabled={run.isPending} onClick={() => start(k)}><Play size={13} />Run</button>}
                  </li>
                ))}
              </ul>
              <div className="mini-stats" style={{ gridTemplateColumns: 'repeat(4, minmax(0, 1fr))' }}>
                <div><b>{Object.keys(d.lab.blocked_ips).length}</b><span>IPs blocked</span></div>
                <div><b>{Object.keys(d.lab.disabled_accounts).length}</b><span>accounts disabled</span></div>
                <div><b>{Object.keys(d.lab.isolated_endpoints ?? {}).length}</b><span>endpoints isolated</span></div>
                <div><b>{Object.keys(d.lab.quarantined_files ?? {}).length}</b><span>files quarantined</span></div>
              </div>
              {Object.entries(d.lab.blocked_ips).map(([ip, b]) => (
                <div key={ip} className="row small"><Ban size={13} /><code>{ip}</code><span className="muted">by {b.by.replace('human:', '')} · {clock(b.at)} · <Link to={`/incidents/${b.incident}`}>{b.incident}</Link></span></div>
              ))}
              {Object.entries(d.lab.disabled_accounts).map(([a, b]) => (
                <div key={a} className="row small"><UserX size={13} /><code>{a}</code><span className="muted">by {b.by.replace('human:', '')} · {clock(b.at)}</span></div>
              ))}
            </div>
          )}
        </Card>
      </div>

      <Card title="Recent authentication events" bodyClass="card-body tight">
        {!d ? <Skeleton h={200} /> : (
          <div className="table-wrap">
            <table className="table">
              <thead><tr><th>Time</th><th>Node</th><th>Event</th><th>User</th><th>Source</th><th className="hide-sm">Detail</th></tr></thead>
              <tbody>
                {d.recent.slice(0, 10).map((e, i) => (
                  <tr key={i}>
                    <td className="mono small nowrap">{clock(e.t)}</td>
                    <td>{e.node}</td>
                    <td><Badge tone={e.kind.includes('fail') || e.kind === 'invalid_user' || e.kind === 'sudo_denied' ? 'warn' : 'neutral'}>{KIND_LABEL[e.kind] ?? e.kind}</Badge></td>
                    <td className="mono small">{e.user ?? '—'}</td>
                    <td className="mono small">{e.src_ip ?? 'local'}</td>
                    <td className="muted small hide-sm">{[e.method, e.detail].filter(Boolean).join(' · ')}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {confirmReset && (
        <Modal title="Reset the lab?" onClose={() => setConfirmReset(false)} footer={<>
          <button className="btn btn-ghost" onClick={() => setConfirmReset(false)}>Cancel</button>
          <button className="btn btn-danger" disabled={reset.isPending} onClick={() => reset.mutate(undefined, {
            onSuccess: () => { toast('good', 'Lab reset'); setConfirmReset(false) }, onError: e => toast('crit', e.message),
          })}>Reset lab</button>
        </>}>
          <p className="ink2">Clears lab events, the lab firewall blocklist and disabled lab accounts. Real telemetry and incidents are not affected.</p>
        </Modal>
      )}
    </main>
  )
}
