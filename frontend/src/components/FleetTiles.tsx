/* The fleet strip, after the Spark Monitor's: four live tiles (online, GPU, memory, models). */
import type { FleetNode } from '../lib/api'
import { Tile } from './Tile'

const gb = (x: number) => x >= 1000 ? (x / 1000).toFixed(2) : x.toFixed(0)

export default function FleetTiles({ fleet }: { fleet?: Record<string, FleetNode> }) {
  const nodes = Object.entries(fleet ?? {})
  const on = nodes.filter(([, n]) => n.ok)
  const util = on.map(([, n]) => n.gpu?.util).filter((u): u is number => u != null)
  const gpu = fleet ? (util.length ? util.reduce((a, b) => a + b, 0) / util.length : null) : null
  const memUsed = on.reduce((a, [, n]) => a + (n.mem_used_gb ?? 0), 0)
  const memTotal = on.reduce((a, [, n]) => a + (n.mem_total_gb ?? 0), 0)
  const servers = on.flatMap(([, n]) => (n.servers ?? []).filter(s => s.status !== 'idle'))   // Ollama models not in memory are not servers
  const ready = servers.filter(s => s.status === 'ready').length

  return (
    <div className="ftiles">
      <Tile i={0} label="Sparks online" color="var(--good)" value={fleet ? on.length : null} of={nodes.length || 4}>
        <div className="fnodes">{nodes.map(([name, n]) => <i key={name} className={n.ok ? 'on' : 'off'} title={`${name}: ${n.ok ? 'online' : 'not responding'}`} />)}</div>
      </Tile>
      <Tile i={1} label="Average GPU load" color="var(--m-gpu)" color2="var(--m-gpu-2)" value={gpu} unit="%" pct={gpu} bump={10} />
      <Tile i={2} label="Memory in use, all Sparks" color="var(--m-mem)" color2="var(--m-mem-2)" value={fleet ? memUsed : null}
        digits={0} of={`${gb(memTotal)} GB`} pct={memTotal ? (100 * memUsed) / memTotal : null} />
      <Tile i={3} label="Models ready, all Sparks" color="var(--good)" color2="var(--good-2)" value={fleet ? ready : null}
        of={servers.length} pct={servers.length ? (100 * ready) / servers.length : null} />
    </div>
  )
}
