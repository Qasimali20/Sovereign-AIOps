import { useMemo, useState } from 'react'
import type { GVertex, VState } from '../lib/api'
import { useImpact, useTopology } from '../lib/queries'
import { Badge, Card, Empty, Page, PageHead, Skeleton } from '../components/ui'

const TONE: Record<VState, 'good' | 'warn' | 'crit'> = { up: 'good', degraded: 'warn', down: 'crit' }
const TYPE: Record<string, string> = { node: 'Node', server: 'Model server', model: 'Model', route: 'Gateway route', component: 'Platform', stack: 'Other team' }
const ORDER = ['component', 'server', 'stack'] as const

function Item({ v, active, onPick }: { v: GVertex; active: boolean; onPick: (id: string) => void }) {
  return (
    <li>
      <button className={`btn btn-sm ${active ? 'btn-primary' : ''}`} style={{ width: '100%', minWidth: 0, justifyContent: 'space-between', gap: 8 }}
        onClick={() => onPick(v.id)} title={v.why || TYPE[v.type]}>
        <span style={{ flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', textAlign: 'left' }}>{v.label}</span>
        {v.state !== 'up' && <Badge tone={TONE[v.state]}>{v.state}</Badge>}
      </button>
    </li>
  )
}

/** Dependency graph as a "what if this fails" explorer: pick anything, see what goes down or degrades with it. */
export default function Topology() {
  const { data } = useTopology()
  const [target, setTarget] = useState<string | null>(null)
  const impact = useImpact(target)
  const by = useMemo(() => {
    const vs = data?.vertices ?? []
    const nodes = vs.filter(v => v.type === 'node')
    return { nodes, shared: vs.filter(v => v.type === 'route' || v.type === 'model'),
      on: (n: string) => ORDER.flatMap(t => vs.filter(v => v.node === n && v.type === t)) }
  }, [data])
  const t = data?.vertices.find(v => v.id === target)

  return (
    <Page>
      <PageHead title="Dependencies" sub="pick a node, server or component to see what fails with it" />
      {data === undefined ? <Skeleton h={300} /> : (
        <div className="grid g-main">
          <div>
            <div className="grid g-2">
              {by.nodes.map(n => (
                <Card key={n.id} title={n.label} right={<button className="btn btn-sm" onClick={() => setTarget(n.id)}>If it fails</button>}>
                  <ul className="items">{by.on(n.label).map(v => <Item key={v.id} v={v} active={v.id === target} onPick={setTarget} />)}</ul>
                </Card>
              ))}
            </div>
            <Card title="Gateway routes and models" hint="routes fall back in order; models balance across replicas">
              <ul className="items">{by.shared.map(v => <Item key={v.id} v={v} active={v.id === target} onPick={setTarget} />)}</ul>
            </Card>
          </div>
          <Card title={t ? `If ${t.label} fails` : 'Impact'} hint={t ? TYPE[t.type] : undefined}>
            {!target ? <Empty>Pick something on the left.</Empty> : impact.data === undefined ? <Skeleton h={120} />
              : impact.data.affected.length === 0 ? <Empty>Nothing else is affected: it has a working fallback or nothing depends on it.</Empty> : (
                <ul className="items">{impact.data.affected.map(a => (
                  <li key={a.id}>
                    <Badge tone={TONE[a.state]}>{a.state}</Badge> <b>{a.label}</b> <span className="muted small">{TYPE[a.type]}{a.node ? ` · ${a.node}` : ''}</span>
                    <div className="small muted">{a.why}</div>
                  </li>
                ))}</ul>
              )}
          </Card>
        </div>
      )}
    </Page>
  )
}
