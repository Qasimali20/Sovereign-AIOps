import { createContext, useCallback, useContext, useEffect, useId, useRef, useState, type ReactNode } from 'react'
import {
  Monitor, Moon, Sun, CheckCircle2, XCircle, Info

} from 'lucide-react'
import { OUTCOME, STATUS_TONE, label, type Tone } from '../lib/format'
import { useTheme, type ThemePref } from '../lib/theme'

// ---------------------------------------------------------------- badges
export function StatusBadge({ status }: { status: string }) {
  return (
    <span className={`badge t-${STATUS_TONE[status] ?? 'neutral'}`}>
      {OUTCOME[status] ?? label(status)}
    </span>
  )
}

/** A coloured dot + label (the tone carries the meaning; text keeps it readable without colour). */
export function Badge({ tone = 'neutral', children }: { tone?: Tone; children: ReactNode }) {
  return <span className={`badge t-${tone}`}>{children}</span>
}

export function PolicyBadge({ decision }: { decision: string }) {
  if (decision === 'deny') return <Badge tone="crit">Denied</Badge>
  if (decision === 'requires_approval') return <Badge tone="warn">Requires approval</Badge>
  return <Badge tone="info">Allowed</Badge>
}

// ---------------------------------------------------------------- layout primitives
export function Card({ title, hint, right, children, className = '', bodyClass = 'card-body' }: {
  title?: ReactNode; hint?: ReactNode; right?: ReactNode; children: ReactNode; className?: string; bodyClass?: string
}) {
  return (
    <section className={`card ${className}`}>
      {(title || right) && (
        <div className="card-head">
          {title && <h2>{title}</h2>}
          {hint && <span className="hint">{hint}</span>}
          {right && <div className="right">{right}</div>}
        </div>
      )}
      <div className={bodyClass}>{children}</div>
    </section>
  )
}

export function PageHead({ title, sub, actions, embedded = false }: { title: ReactNode; sub?: ReactNode; actions?: ReactNode; embedded?: boolean }) {
  if (embedded) {   // inside a tabbed page: the tab is the title; keep the description and actions
    return (sub || actions) ? <div className="tab-head">{sub && <div className="sub">{sub}</div>}{actions && <div className="tab-actions">{actions}</div>}</div> : null
  }
  return (
    <div className="page-head">
      <div>
        <h1>{title}</h1>
        {sub && <div className="sub">{sub}</div>}
      </div>
      {actions && <div className="actions">{actions}</div>}
    </div>
  )
}

/** Page body: a <main> on its own, a plain stack when embedded in a tabbed page. */
export function Page({ embedded = false, children }: { embedded?: boolean; children: ReactNode }) {
  return embedded ? <div className="tab-body">{children}</div> : <main className="content">{children}</main>
}

/** A page made of tabs (?tab=…): one heading, a tab row, then the chosen section. */
export function TabbedPage({ title, tabs, current, onChange, children }: {
  title: string; tabs: { key: string; label: string }[]; current: string; onChange: (k: string) => void; children: ReactNode
}) {
  return (
    <main className="content">
      <div className="page-head"><div><h1>{title}</h1></div></div>
      <div className="tabs" role="tablist">
        {tabs.map(t => <button key={t.key} role="tab" aria-selected={current === t.key} className={current === t.key ? 'on' : ''} onClick={() => onChange(t.key)}>{t.label}</button>)}
      </div>
      {children}
    </main>
  )
}

export type Metric = 'gpu' | 'mem' | 'cpu' | 'disk'

export function Kpi({ label: l, value, unit, foot, metric }: { label: ReactNode; value: ReactNode; unit?: string; foot?: ReactNode; metric?: Metric }) {
  return (
    <div className={`card kpi${metric ? ` m-${metric}` : ''}`}>
      <div className="label">{l}</div>
      <div className="value">{value}{unit && <small>{unit}</small>}</div>
      {foot && <div className="foot">{foot}</div>}
    </div>
  )
}

export function Empty({ icon, children }: { icon?: ReactNode; children: ReactNode }) {
  return <div className="empty">{icon}<div>{children}</div></div>
}

export function Skeleton({ h = 16, w = '100%' }: { h?: number; w?: number | string }) {
  return <div className="skeleton" style={{ height: h, width: w }} />
}

export function Tabs<K extends string>({ tabs, value, onChange }: {
  tabs: { key: K; label: ReactNode; count?: number }[]; value: K; onChange: (k: K) => void
}) {
  return (
    <div className="tabs" role="tablist">
      {tabs.map(t => (
        <button key={t.key} role="tab" aria-selected={value === t.key} className={`tab${value === t.key ? ' on' : ''}`} onClick={() => onChange(t.key)}>
          {t.label}{t.count != null && <span className="n">{t.count}</span>}
        </button>
      ))}
    </div>
  )
}

export function Segmented<K extends string>({ options, value, onChange }: { options: { key: K; label: string }[]; value: K; onChange: (k: K) => void }) {
  return (
    <div className="seg">
      {options.map(o => <button key={o.key} type="button" aria-pressed={value === o.key} className={value === o.key ? 'on' : ''} onClick={() => onChange(o.key)}>{o.label}</button>)}
    </div>
  )
}

export function Modal({ title, children, onClose, footer }: { title: ReactNode; children: ReactNode; onClose: () => void; footer: ReactNode }) {
  const id = useId(), box = useRef<HTMLDivElement>(null), close = useRef(onClose)
  close.current = onClose                          // callers pass inline functions: read the latest, run the effect once
  useEffect(() => {
    const back = document.activeElement as HTMLElement | null
    box.current?.querySelector<HTMLElement>('input, textarea, select, button')?.focus()
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') close.current() }
    document.addEventListener('keydown', onKey)
    return () => { document.removeEventListener('keydown', onKey); back?.focus() }
  }, [])
  return (
    <div className="modal-back" onMouseDown={e => { if (e.target === e.currentTarget) onClose() }}>
      <div className="modal" role="dialog" aria-modal="true" aria-labelledby={id} ref={box}>
        <div className="card-head"><h2 id={id}>{title}</h2></div>
        <div className="card-body">{children}</div>
        <div className="modal-foot">{footer}</div>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------- toasts
type Toast = { id: number; tone: 'good' | 'crit' | 'info'; text: string }
const ToastCtx = createContext<(tone: Toast['tone'], text: string) => void>(() => undefined)
export const useToast = () => useContext(ToastCtx)

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<Toast[]>([])
  const push = useCallback((tone: Toast['tone'], text: string) => {
    const id = Date.now() + Math.random()
    setItems(x => [...x, { id, tone, text }])
    window.setTimeout(() => setItems(x => x.filter(t => t.id !== id)), 4500)
  }, [])
  return (
    <ToastCtx.Provider value={push}>
      {children}
      <div className="toasts" aria-live="polite">
        {items.map(t => (
          <div key={t.id} className="toast">
            {t.tone === 'good' ? <CheckCircle2 size={16} color="var(--good-ink)" /> : t.tone === 'crit' ? <XCircle size={16} color="var(--crit-ink)" /> : <Info size={16} color="var(--info-ink)" />}
            {t.text}
          </div>
        ))}
      </div>
    </ToastCtx.Provider>
  )
}

// ---------------------------------------------------------------- theme switcher
const THEMES: { key: ThemePref; label: string; icon: ReactNode }[] = [
  { key: 'light', label: 'Light', icon: <Sun size={15} /> },
  { key: 'dark', label: 'Dark', icon: <Moon size={15} /> },
  { key: 'system', label: 'System', icon: <Monitor size={15} /> },
]

export function ThemeSwitcher() {
  const { pref, setPref, resolved } = useTheme()
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!open) return
    const h = (e: MouseEvent) => { if (!ref.current?.contains(e.target as Node)) setOpen(false) }
    document.addEventListener('mousedown', h)
    return () => document.removeEventListener('mousedown', h)
  }, [open])
  return (
    <div className="menu" ref={ref}>
      <button className="icon-btn" aria-label={`Theme: ${pref}`} title={`Theme: ${pref}`} onClick={() => setOpen(o => !o)}>
        {resolved === 'dark' ? <Moon size={16} /> : <Sun size={16} />}
      </button>
      {open && (
        <div className="menu-pop" style={{ minWidth: 160 }}>
          <div className="mlabel">Appearance</div>
          {THEMES.map(t => (
            <button key={t.key} className={`mi${pref === t.key ? ' on' : ''}`} onClick={() => { setPref(t.key); setOpen(false) }}>
              {t.icon}{t.label}{pref === t.key && <CheckCircle2 size={14} style={{ marginLeft: 'auto' }} />}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------- model kind tag
export const KINDS = ['LLM', 'VLM', 'OCR', 'ASR', 'TTS', 'Embed', 'Rerank'] as const

export function KindTag({ kind, count }: { kind: string; count?: number }) {
  return <span className={`badge kind kind-${kind}`}>{kind}{count != null && <b>×{count}</b>}</span>
}
