import { Badge, Card, Page, PageHead, Skeleton } from '../components/ui'
import { useCapacity } from '../lib/queries'
import { ago } from '../lib/format'

const gb = (v: number | null | undefined) => v == null ? '–' : `${v.toFixed(v < 10 ? 1 : 0)} GB`
const days = (v: number | null) => v == null ? 'not on current trend' : v > 3650 ? 'over 10 years' : v > 365 ? `${(v / 365).toFixed(1)} years` : `${Math.round(v)} days`

/** Capacity: room on each node now, how fast disks fill, and how traffic to each model is moving. */
export default function Capacity() {
  const { data } = useCapacity()
  return (
    <Page>
      <PageHead title="Capacity" sub={data ? `computed ${ago(data.generated)} · forecasts extrapolate the last week` : undefined} />
      {data === undefined ? <Skeleton h={300} /> : (
        <>
          <Card title="Memory" hint="unified memory: weights, KV cache and everything else on the node share it" bodyClass="card-body tight">
            <div className="table-wrap">
              <table className="table">
                <thead><tr><th>Node</th><th className="num">In use</th><th className="num">Free now</th><th className="num hide-sm">7-day peak</th>
                  <th>A model server starting now can reserve</th><th className="hide-sm">Reserved by running servers</th></tr></thead>
                <tbody>{Object.entries(data.nodes).map(([n, r]) => {
                  const m = r.memory
                  if (!m) return <tr key={n}><td>{n}</td><td colSpan={5} className="muted">not reported</td></tr>
                  return (
                    <tr key={n}>
                      <td><b>{n}</b></td>
                      <td className="num nowrap">{gb(m.used_gb)} <span className="muted small">of {gb(m.total_gb)}</span></td>
                      <td className="num nowrap">{gb(m.free_gb)} {m.tight && <Badge tone="warn">tight</Badge>}</td>
                      <td className="num nowrap hide-sm">{gb(m.peak_gb_7d)}</td>
                      <td className="small">up to <b>{m.max_new_share.toFixed(2)}</b> (--gpu-memory-utilization, {gb(m.max_new_gb)})
                        {m.tight && <div className="muted">restarting any model server here may fail if it needs more than this</div>}</td>
                      <td className="small hide-sm">{m.reserved.map(x => `${x.model} ${x.share ?? '?'}`).join(' · ') || '–'}
                        {m.unknown_shares.length > 0 && <div className="muted">? = not reported (multi-stage servers can hold more than their flag)</div>}</td>
                    </tr>
                  )
                })}</tbody>
              </table>
            </div>
          </Card>

          <Card title="Disk" hint={`incident at ${data.disk_warn_pct}%; model weights arrive in jumps, so both pace and jumps are shown`} bodyClass="card-body tight">
            <div className="table-wrap">
              <table className="table">
                <thead><tr><th>Node</th><th className="num">Used</th><th className="num">Typical change per day</th>
                  <th className="num">Until {data.disk_warn_pct}% at that pace</th><th className="num hide-sm">Largest one-day jump</th>
                  <th className="num hide-sm">More jumps like it before {data.disk_warn_pct}%</th></tr></thead>
                <tbody>{Object.entries(data.nodes).map(([n, r]) => {
                  const d = r.disk
                  if (!d) return <tr key={n}><td>{n}</td><td colSpan={5} className="muted">not reported</td></tr>
                  return (
                    <tr key={n}>
                      <td><b>{n}</b></td>
                      <td className="num nowrap">{d.pct.toFixed(0)}% <span className="muted small">{(d.used_gb / 1000).toFixed(2)} of {(d.total_gb / 1000).toFixed(1)} TB</span></td>
                      <td className="num nowrap">{d.growth_gb_per_day == null ? <span className="muted">needs 3 days</span> : `${d.growth_gb_per_day >= 0 ? '+' : ''}${d.growth_gb_per_day.toFixed(1)} GB`}</td>
                      <td className="num nowrap">{days(d.days_to_warn)}</td>
                      <td className="num nowrap hide-sm">{gb(d.largest_jump_gb)}</td>
                      <td className="num hide-sm">{d.jumps_left ?? '–'}</td>
                    </tr>
                  )
                })}</tbody>
              </table>
            </div>
          </Card>

          <Card title="Model traffic" hint="requests per day (oldest first), and the busiest each server has been this week" bodyClass="card-body tight">
            <div className="table-wrap">
              <table className="table">
                <thead><tr><th>Server</th><th>Requests per day</th><th className="num">Trend</th><th className="num">Peak in flight</th><th className="num">Peak KV cache</th></tr></thead>
                <tbody>{data.models.map(m => (
                  <tr key={`${m.node}/${m.model}`}>
                    <td><b>{m.model}</b><div className="sub">{m.node}{m.kind ? ` · ${m.kind}` : ''}</div></td>
                    <td className="mono small">{m.per_day.map(v => Math.round(v)).join(' · ') || '–'}</td>
                    <td className="num nowrap">{m.change_pct == null ? <span className="muted">needs 5 days</span> : `${m.change_pct > 0 ? '+' : ''}${Math.round(m.change_pct)}% over the week`}</td>
                    <td className="num">{m.peak_running_7d == null ? '–' : Math.round(m.peak_running_7d)}</td>
                    <td className="num">{m.peak_kv_7d == null ? '–' : `${Math.round(m.peak_kv_7d * 100)}%`}</td>
                  </tr>
                ))}</tbody>
              </table>
            </div>
          </Card>
        </>
      )}
    </Page>
  )
}
