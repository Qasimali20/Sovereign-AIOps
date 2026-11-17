import { useEffect, useState } from 'react'
import { useLocation } from 'react-router-dom'
import { AlertTriangle, CheckCircle2 } from 'lucide-react'
import type { Container, NodeServices } from '../lib/api'
import { useServices } from '../lib/queries'
import { Badge, Card, PageHead, Segmented, Skeleton } from '../components/ui'
import { Tile } from '../components/Tile'
import { ago } from '../lib/format'

function ContainerState({ c }: { c: Container }) {
  if (c.health === 'unhealthy') return <Badge tone="crit">Unhealthy</Badge>
  if (c.state === 'restarting') return <Badge tone="warn">Restarting</Badge>
  if (c.state !== 'running') return <Badge>{c.state === 'exited' ? 'Stopped' : c.state}</Badge>
  if (c.health === 'healthy') return <Badge tone="good">Healthy</Badge>
  if (c.health === 'starting') return <Badge tone="info">Starting</Badge>
  return <Badge tone="good">Running</Badge>
}

const problem = (c: Container) => c.health === 'unhealthy' || c.state === 'restarting'

function NodeCard({ name, n, view }: { name: string; n: NodeServices; view: 'running' | 'problems' | 'all' }) {
  const rows = n.containers.filter(c => view === 'all' || (view === 'problems' ? problem(c) : c.state === 'running'))
  const s = n.summary
  const units = (n.problem_units ?? []).filter(u => view === 'all' || !u.known)
  return (
    <Card title={name} hint={n.ok ? `checked ${ago(n.checked ?? 0)}` : 'not reachable'}
      right={s.ok ? <span className="small muted">{s.containers_running} containers running</span> : undefined}
      bodyClass="card-body tight">
      {!n.ok ? <div className="empty-inline"><AlertTriangle size={18} />{n.error}</div> : (
        <>
          {!n.docker_ok && (
            <div className="callout warn" style={{ margin: 14 }}><AlertTriangle size={16} />
              <div>Containers on {name} are not visible: the monitoring account is not in the <code>docker</code> group there. systemd services are still monitored.</div></div>
          )}
          {rows.length > 0 && (
            <table className="table">
              <thead><tr><th>Container</th><th>State</th><th className="hide-sm">Uptime</th></tr></thead>
              <tbody>{rows.map(c => (
                <tr key={c.name}><td className="primary nowrap">{c.name}</td><td><ContainerState c={c} /></td>
                  <td className="muted small hide-sm">{c.status.replace(/\s*\((healthy|unhealthy|health: starting)\)/, '').replace(/^Up /, '')}</td></tr>
              ))}</tbody>
            </table>
          )}
          {n.docker_ok && rows.length === 0 && view === 'problems' && <div className="empty-inline"><CheckCircle2 size={18} />No container problems.</div>}
          {units.length > 0 && <div className="svc-units">
            {(
              <div><h3>Failed or restarting</h3>
                {units.map(u => (
                  <div key={u.unit} className="row small" style={{ marginTop: 6 }}>
                    {u.known ? <Badge>Known</Badge> : <Badge tone="crit">{u.sub === 'auto-restart' ? 'Restarting' : 'Failed'}</Badge>}
                    <code>{u.unit}</code>{u.known && <span className="muted">{u.known}</span>}
                  </div>
                ))}
              </div>
            )}
          </div>}
        </>
      )}
    </Card>
  )
}

export default function Services() {
  const { hash } = useLocation()
  const { data } = useServices()
  const loaded = data !== undefined
  // links like /services#Node-2 (from the dashboard) scroll to that node once, when its card first appears
  useEffect(() => { if (hash && loaded) document.getElementById(decodeURIComponent(hash.slice(1)))?.scrollIntoView({ block: 'start' }) }, [hash, loaded])
  const [view, setView] = useState<'running' | 'problems' | 'all'>('running')
  const nodes = data ? Object.values(data) : []
  const sum = (k: 'containers_running' | 'containers_unhealthy' | 'containers_restarting' | 'units_failed') =>
    nodes.reduce((a, n) => a + (n.summary[k] ?? 0), 0)
  const running = sum('containers_running'), unhealthy = sum('containers_unhealthy')
  const restarting = sum('containers_restarting'), failed = sum('units_failed')
  const total = nodes.reduce((a, n) => a + n.containers.length, 0)

  return (
    <main className="content">
      <PageHead title="Services"
        actions={<Segmented value={view} onChange={setView} options={[{ key: 'running', label: 'Running' }, { key: 'problems', label: 'Problems' }, { key: 'all', label: 'All' }]} />} />
      <div className="ftiles">
        <Tile i={0} label="Containers running" color="var(--good)" value={data ? running : null}
          foot={data ? `across ${nodes.length} nodes · ${total - running} stopped` : undefined} />
        {/* problem tiles take their alarm colour only when there is something to see */}
        <Tile i={1} label="Unhealthy" color={unhealthy ? 'var(--crit)' : 'var(--m-bar)'} value={data ? unhealthy : null} foot="failing their health check" />
        <Tile i={2} label="Restarting" color={restarting ? 'var(--warn)' : 'var(--m-bar)'} value={data ? restarting : null} foot="containers in a restart loop" />
        <Tile i={3} label="Failed services" color={failed ? 'var(--crit)' : 'var(--m-bar)'} value={data ? failed : null} foot="systemd units, excluding known-benign" />
      </div>
      <div className="grid g-2">
        {data ? Object.entries(data).map(([name, n]) => <div key={name} id={name} className="anchor-target"><NodeCard name={name} n={n} view={view} /></div>)
          : [0, 1, 2, 3].map(i => <Card key={i}><Skeleton h={200} /></Card>)}
      </div>
    </main>
  )
}
