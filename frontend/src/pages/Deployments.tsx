import { Rocket } from 'lucide-react'
import { useDeployments } from '../lib/queries'
import { Badge, Card, Empty, Page, PageHead, Skeleton } from '../components/ui'
import { ago, dateTime, duration } from '../lib/format'

const TONE = { running: 'info', succeeded: 'good', failed: 'crit' } as const

/** Controlled deployments: each previewed, recorded, applied in a maintenance window and checked for drift. */
export default function Deployments({ embedded = false }: { embedded?: boolean }) {
  const { data } = useDeployments()
  return (
    <Page embedded={embedded}>
      <PageHead embedded={embedded} title="Deployments" />
      <Card title="Deployments" hint="ops/scripts/deploy.py: preview, confirm, record, apply, verify" bodyClass="card-body tight">
        {data === undefined ? <div className="card-body"><Skeleton h={160} /></div> : data.length === 0 ? (
          <Empty icon={<Rocket size={28} />}>No deployment has been recorded yet.</Empty>
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead><tr><th>Deployment</th><th>Outcome</th><th>Nodes</th><th className="hide-sm">Commit</th>
                <th className="hide-sm">By</th><th className="num hide-sm">Took</th><th>Drift after</th><th className="hide-sm">Started</th></tr></thead>
              <tbody>{data.map(d => {
                const drift = Object.entries(d.result?.drift ?? {}).filter(([, k]) => k.length)
                return (
                  <tr key={d.id}>
                    <td><b>{d.playbook}</b><div className="sub">{d.id} · {d.reason}</div></td>
                    <td><Badge tone={TONE[d.status]}>{d.status}</Badge></td>
                    <td className="nowrap">{d.nodes.join(', ')}</td>
                    <td className="mono small hide-sm">{d.git_commit}</td>
                    <td className="small hide-sm">{d.by.replace('human:', '')}</td>
                    <td className="num nowrap hide-sm">{d.status === 'running' ? '…' : duration(d.updated - d.created)}</td>
                    <td className="small">{d.result == null ? '–' : drift.length === 0 ? 'none'
                      : drift.map(([n, k]) => `${n}: ${k.length} item${k.length > 1 ? 's' : ''}`).join(' · ')}</td>
                    <td className="muted nowrap hide-sm" title={dateTime(d.created)}>{ago(d.created)}</td>
                  </tr>
                )
              })}</tbody>
            </table>
          </div>
        )}
      </Card>
    </Page>
  )
}
