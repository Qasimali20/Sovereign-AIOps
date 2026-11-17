import { useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { RANGES, type Range } from '../components/MetricPanel'

/** The page's time range, kept in the URL (?range=) so a link or reload keeps it. */
export function useRange(): [Range, (r: Range) => void] {
  const [params, setParams] = useSearchParams()
  const [range, setRange] = useState<Range>((RANGES.find(r => r.key === params.get('range'))?.key) ?? '6h')
  return [range, (r: Range) => { setRange(r); setParams(p => { p.set('range', r); return p }, { replace: true }) }]
}
