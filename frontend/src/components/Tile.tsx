/* Animated KPI tile, after the Spark Monitor's: tiles rise in one after another, the number counts to its value,
 * a gradient track fills, a soft glow in the corner carries the tile's colour. Respects prefers-reduced-motion. */
import { useEffect, useRef, useState, type CSSProperties, type ReactNode } from 'react'

const ease = (k: number) => 1 - Math.pow(1 - k, 3)
const reduced = () => typeof matchMedia === 'function' && matchMedia('(prefers-reduced-motion: reduce)').matches

/** The displayed number glides to `to` (900 ms); `bump` > 0 flashes it when it jumps by at least that much. */
function useTween(to: number | null, bump = 0): [number | null, boolean] {
  const [cur, setCur] = useState<number | null>(null)
  const [flash, setFlash] = useState(false)
  const last = useRef<number | null>(null)
  useEffect(() => {
    const from = last.current ?? 0                          // first value: count up from zero
    if (to == null || reduced() || from === to) { last.current = to; setCur(to); return }
    let off = 0
    if (bump && last.current != null && Math.abs(to - from) >= bump) { setFlash(true); off = window.setTimeout(() => setFlash(false), 1000) }
    const t0 = performance.now()
    let raf = 0
    const step = (now: number) => {
      const k = Math.min(1, (now - t0) / 900), x = from + (to - from) * ease(k)
      last.current = x; setCur(x)
      if (k < 1) raf = requestAnimationFrame(step)
    }
    raf = requestAnimationFrame(step)
    return () => { cancelAnimationFrame(raf); clearTimeout(off) }
  }, [to, bump])
  return [cur, flash]
}

/** One tile: label, a number that counts to its value, then a gradient track (`pct`), a foot line, or `children`.
 *  `fmt` renders the (moving) number; `i` staggers the entrance. */
export function Tile({ i, label, color, color2, value, of, unit, pct, bump, digits = 0, fmt, foot, title, children }: {
  i: number; label: ReactNode; color: string; color2?: string; value: number | null; of?: ReactNode; unit?: string
  pct?: number | null; bump?: number; digits?: number; fmt?: (v: number) => string; foot?: ReactNode; title?: string; children?: ReactNode
}) {
  const [v, flash] = useTween(value, bump)
  return (
    <div className="ftile" title={title} style={{ '--i': i, '--k': color, '--k2': color2 ?? color } as CSSProperties}>
      <span>{label}</span>
      <b className={flash ? 'bump' : undefined}>
        {v == null ? '–' : fmt ? fmt(v) : v.toFixed(digits)}{unit && <small>{unit}</small>}{of != null && <small>of {of}</small>}
      </b>
      {children ?? (pct !== undefined && <div className="ftrack"><i style={{ width: `${Math.max(0, Math.min(100, pct ?? 0))}%` }} /></div>)}
      {foot && <small className="ffoot">{foot}</small>}
    </div>
  )
}
