import { useEffect, useId, useRef, useState } from 'react'
import { severity } from '../lib/format'
/* Ring gauge in the Spark Monitor style: a 270° outer arc for GPU load, an inner arc for memory,
 * the GPU value in the centre. Colours come from the metric tokens (--m-gpu, --m-mem); the memory arc
 * turns warn/crit at the detector's thresholds so the ring itself says when a node is near trouble. */

const RO = 88, RI = 68, SWEEP = 0.75
const CO = 2 * Math.PI * RO, CI = 2 * Math.PI * RI
const dash = (c: number, p: number) => `${Math.max(0, Math.min(1, p)) * SWEEP * c} ${c}`


/** The GPU arc and its tip glide to a new value along the arc (1.1 s), as on the Spark Monitor. */
function useSweep(to: number): number {
  const [cur, setCur] = useState(0)
  const last = useRef(0)
  useEffect(() => {
    const from = last.current
    if (from === to || (typeof matchMedia === 'function' && matchMedia('(prefers-reduced-motion: reduce)').matches)) {
      last.current = to; setCur(to); return
    }
    const t0 = performance.now()
    let raf = 0
    const step = (now: number) => {
      const k = Math.min(1, (now - t0) / 1100), x = from + (to - from) * (1 - Math.pow(1 - k, 3))
      last.current = x; setCur(x)
      if (k < 1) raf = requestAnimationFrame(step)
    }
    raf = requestAnimationFrame(step)
    return () => cancelAnimationFrame(raf)
  }, [to])
  return cur
}

/** `size` in px; leave it out to size the ring from CSS (--g). */
export function Ring({ gpu, mem, size, label = 'GPU load', foot, busy = false }: {
  gpu: number | null | undefined; mem: number | null | undefined; size?: number; label?: string; foot?: React.ReactNode; busy?: boolean
}) {
  const id = useId().replace(/[^a-zA-Z0-9]/g, '')          // gradient ids must be unique per ring
  const g = useSweep(Math.max(0, Math.min(100, gpu ?? 0)) / 100)
  const m = Math.max(0, Math.min(100, mem ?? 0)) / 100
  const a = g * SWEEP * 2 * Math.PI
  const memLevel = severity(mem, 80, 92)
  return (
    <div className={`ring${busy ? ' busy' : ''}`} style={size ? { ['--g' as string]: `${size}px` } : undefined}>
      <div className="ring-box">
        <svg viewBox="0 0 220 220" aria-hidden="true">
          <defs>
            <linearGradient id={`g${id}`} x1="0" y1="1" x2="1" y2="0"><stop offset="0" style={{ stopColor: 'var(--m-gpu-2)' }} /><stop offset="1" style={{ stopColor: 'var(--m-gpu)' }} /></linearGradient>
            <linearGradient id={`m${id}`} x1="0" y1="1" x2="1" y2="0"><stop offset="0" style={{ stopColor: 'var(--m-mem-2)' }} /><stop offset="1" style={{ stopColor: 'var(--m-mem)' }} /></linearGradient>
          </defs>
          <g transform="rotate(135 110 110)">
            <circle className="trk" cx="110" cy="110" r={RO} strokeWidth="10" strokeDasharray={dash(CO, 1)} />
            <circle className="arc gpu" cx="110" cy="110" r={RO} strokeWidth="10" strokeDasharray={dash(CO, g)} stroke={`url(#g${id})`} />
            <circle className="trk" cx="110" cy="110" r={RI} strokeWidth="6" strokeDasharray={dash(CI, 1)} />
            <circle className={`arc mem ${memLevel}`} cx="110" cy="110" r={RI} strokeWidth="6" strokeDasharray={dash(CI, m)}
              stroke={memLevel ? undefined : `url(#m${id})`} />
            {gpu != null && <circle className="tip" cx={110 + RO * Math.cos(a)} cy={110 + RO * Math.sin(a)} r="5" />}
          </g>
        </svg>
        <div className="ring-c">
          <div className="v">{gpu == null ? '—' : Math.round(gpu)}<small>%</small></div>
          <div className="l">{label}</div>
        </div>
      </div>
      {foot && <div className="ring-foot">{foot}</div>}
    </div>
  )
}

/** Label, value and a thin bar underneath (Spark Monitor's stat rows). `pct` scales the bar. */
export function StatBar({ label, value, pct, lvl = '' }: { label: string; value: string; pct: number | null | undefined; lvl?: string }) {
  return (
    <div className="statbar">
      <div className="sb-row"><span>{label}</span><b className={lvl}>{value}</b></div>
      <div className="sb-track"><i className={lvl} style={{ width: `${Math.max(0, Math.min(100, pct ?? 0))}%` }} /></div>
    </div>
  )
}
