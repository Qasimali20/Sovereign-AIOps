import type { CSSProperties } from 'react'
import { Link } from 'react-router-dom'
import { ArrowDown, ArrowUp, EyeOff, FileWarning, Network } from 'lucide-react'
import type { FleetNode, SystemInfo } from '../lib/api'
import { useFleet, useNodeHistory, useSystem } from '../lib/queries'
import { MiniLines } from '../components/charts'
import { Ring, StatBar } from '../components/gauges'
import { bytesPerSec, severity as level, uptime } from '../lib/format'
import { Badge, PageHead, Skeleton } from '../components/ui'
import MetricBoard, { RangePicker } from '../components/MetricBoard'
import { useRange } from '../lib/useRange'
import FleetTiles from '../components/FleetTiles'
import { RANGES, type Range } from '../components/MetricPanel'

type Coverage = SystemInfo['kernel_coverage'][string] | undefined

const rate = (b: number | null | undefined) => bytesPerSec(b)
const size = (gb: number | null | undefined) => gb == null ? '—' : gb >= 1000 ? `${(gb / 1000).toFixed(1)} TB` : `${gb} GB`

function NodeCard({ name, n, cov, range, i }: { name: string; n: FleetNode; cov: Coverage; range: Range; i: number }) {
  const hist = useNodeHistory(name, range)
  const pts = (hist.data?.points ?? []).map(p => ({ t: p[0], gpu: p[1], cpu: p[2] }))
  const mem = n.mem_total_gb ? (100 * (n.mem_used_gb ?? 0)) / n.mem_total_gb : null
  const servers = (n.servers ?? []).filter(s => s.status !== 'idle')   // Ollama models not in memory are not servers
  const ready = servers.filter(s => s.status === 'ready')
  // "In use" is the GPU's own reading: engines' queue counts can stick (vLLM-Omni TTS keeps finished requests counted)
  const active = (n.gpu?.util ?? 0) >= 10
  const status = !n.ok ? { cls: 'down', text: 'Unreachable' } : active ? { cls: 'busy', text: 'In use' }
    : ready.length ? { cls: 'serve', text: 'Serving' } : { cls: 'idle', text: 'Idle' }
  const viaTs = n.via?.includes('//100.')
  const g = n.gpu

  return (
    <Link to={`/fleet/${name}`} className={`card spark${active ? ' busy' : ''}${!n.ok ? ' off' : ''}`} style={{ '--i': i } as CSSProperties}>
      <header className="spark-h">
        <div>
          <h2>{name}</h2>
          <div className="sub"><span className="mono">{n.host}</span>{n.ok && <> · up {uptime(n.uptime_h)}</>}</div>
        </div>
        <span className={`nstate ${status.cls}`}><i />{status.text}</span>
      </header>

      {!n.ok ? <div className="spark-off"><EyeOff size={28} /><p>{n.error ?? 'Not responding'}</p></div> : (
        <>
          <div className="spark-top">
            <Ring gpu={g?.util} mem={mem} busy={active}
              foot={<b>RAM {n.mem_used_gb?.toFixed(0)} / {n.mem_total_gb?.toFixed(0)} GB</b>} />
            <div className="spark-stats">
              <StatBar label="GPU temp" value={g?.temp != null ? `${g.temp.toFixed(0)}°C` : '—'} pct={g?.temp} lvl={level(g?.temp, 80, 88)} />
              <StatBar label="GPU power" value={g?.power != null ? `${g.power.toFixed(0)} W` : '—'} pct={g?.power != null ? g.power / 1.4 : null} />
              <StatBar label="CPU load" value={n.cpu_percent != null ? `${n.cpu_percent.toFixed(0)}%` : '—'} pct={n.cpu_percent} lvl={level(n.cpu_percent, 85, 97)} />
              <StatBar label="CPU temp" value={n.cpu_temp_c != null ? `${n.cpu_temp_c.toFixed(0)}°C` : '—'} pct={n.cpu_temp_c} lvl={level(n.cpu_temp_c, 85, 95)} />
            </div>
          </div>

          <div className="spark-chart">
            <div className="cl">{RANGES.find(r => r.key === range)?.label} <i style={{ background: 'var(--m-gpu)' }} />GPU <i style={{ background: 'var(--m-cpu)' }} />CPU</div>
            {hist.data ? <MiniLines data={pts} lines={[{ k: 'gpu', color: 'var(--m-gpu)', fill: true }, { k: 'cpu', color: 'var(--m-cpu)' }]} height={150} />
              : <Skeleton h={150} />}
          </div>

          <div className="spark-duo">
            <div className="tile">
              <div className="tr"><span>Disk</span><b className={level(n.disk_used_pct, 85, 95)}>{n.disk_used_pct?.toFixed(0) ?? '—'}%</b></div>
              <div className="sb-track"><i className={level(n.disk_used_pct, 85, 95)} style={{ width: `${n.disk_used_pct ?? 0}%`, background: 'var(--m-disk)' }} /></div>
              <small>{size(n.disk_used_gb)} of {size(n.disk_total_gb)}</small>
            </div>
            <div className="tile">
              <div className="tr"><span><ArrowDown size={12} /> in</span><b>{rate(n.net_rx_bps)}</b></div>
              <div className="tr"><span><ArrowUp size={12} /> out</span><b>{rate(n.net_tx_bps)}</b></div>
              {viaTs && <small className="row" style={{ gap: 4 }}><Network size={12} />reached via Tailscale</small>}
            </div>
          </div>

          {cov && !cov.ok && <Badge tone="serious">GPU driver log · no access</Badge>}

        </>
      )}
    </Link>
  )
}

export default function Fleet() {
  const { data } = useFleet()
  const cov = useSystem().data?.kernel_coverage ?? {}
  const gaps = Object.entries(cov).filter(([, c]) => !c.ok)
  const [range, setRange] = useRange()
  return (
    <main className="content">
      <PageHead title="Fleet" actions={<RangePicker range={range} onChange={setRange} />} />
      <FleetTiles fleet={data} />
      <div className="grid g-2 spark-grid">
        {data ? Object.entries(data).map(([name, n], i) => <NodeCard key={name} name={name} n={n} cov={cov[name]} range={range} i={i} />)
          : [0, 1, 2, 3].map(i => <div key={i} className="card spark"><Skeleton h={20} w="30%" /><Skeleton h={160} /></div>)}
      </div>
      {gaps.length > 0 && (
        <div className="callout warn">
          <FileWarning size={16} />
          <div>
            <b>GPU driver events are not visible on {gaps.map(([n]) => n).join(' and ')}.</b> A GPU firmware hang (Xid 119)
            on {gaps.length === 1 ? 'this node' : 'these nodes'} would not raise an incident. The platform reads the kernel
            journal over SSH; authorise its public key for the <code>tmc</code> account on {gaps.length === 1 ? 'that node' : 'those nodes'}.
            <div className="muted small" style={{ marginTop: 4 }}>{gaps.map(([n, c]) => `${n}: ${c.error}`).join(' · ')}</div>
          </div>
        </div>
      )}
      <MetricBoard board="fleet" range={range} stats={false} />
    </main>
  )
}
