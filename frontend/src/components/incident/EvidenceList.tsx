import { ChevronRight } from 'lucide-react'
import type { Bundle } from '../../lib/api'
import { Card } from '../ui'
import { EvidenceBody } from '../logs'
import { clock } from '../../lib/format'

/** Every evidence record the investigation collected; `flash` opens and highlights one (from a citation link). */
export function EvidenceList({ b, flash }: { b: Bundle; flash: string | null }) {
  return (
    <Card title="Evidence">
      {b.evidence_records.map(e => (
        <details key={e.id} id={e.id} className={`ev${flash === e.id ? ' flash' : ''}`} open={flash === e.id || undefined}>
          <summary>
            <ChevronRight size={15} className="chev" />
            <span className="chip">{e.id}</span><b className="ev-tool">{e.tool}</b>
            <span className="muted small hide-sm mono ev-source">{e.source}</span>
            <span className="spacer" /><span className="muted small">{clock(e.created)}</span>
          </summary>
          <EvidenceBody tool={e.tool} data={e.data} />
        </details>
      ))}
    </Card>
  )
}
