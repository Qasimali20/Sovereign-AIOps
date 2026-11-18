import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { Link, NavLink, useLocation, useNavigate } from 'react-router-dom'
import { GitCompare, Gauge,
  BarChart3, BookOpenCheck, PanelLeftClose, PanelLeftOpen, Boxes, ClipboardCheck, Cpu, LayoutDashboard, LogOut, Menu, Search, ShieldCheck, ShieldAlert, UserCog, Users, Siren, Container,
} from 'lucide-react'
import { DEMO, type IncidentRow, type SystemInfo, type User } from '../lib/api'
import { useApprovals, useIncidentSummary, useIncidents, useSystem } from '../lib/queries'
import { StatusBadge, ThemeSwitcher } from './ui'
import { ago } from '../lib/format'

function NavItem({ to, icon, children, count, alert, end }: { to: string; icon: ReactNode; children: ReactNode; count?: number; alert?: boolean; end?: boolean }) {
  return (
    <NavLink to={to} end={end} title={typeof children === 'string' ? children : undefined} className={({ isActive }) => `sb-link${isActive ? ' active' : ''}`}>
      {icon}<span className="sb-label">{children}</span>{count ? <span className={`sb-count${alert ? ' alert' : ''}`}>{count}</span> : null}
    </NavLink>
  )
}

function SystemFoot({ sys }: { sys: SystemInfo | null }) {
  if (DEMO) return (                         // an offline snapshot never claims to be live
    <div className="sb-foot"><div className="row" title="Recorded from the live platform; read-only">
      <span className="live-dot warn" /><span className="sb-label">Demo snapshot · {new Date().toLocaleString([], { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' })}</span>
    </div></div>
  )
  if (!sys) return null
  const llmOk = sys.llm_endpoints.some(e => e.healthy)
  const live = sys.detector.last != null && Date.now() / 1000 - sys.detector.last < sys.detector.interval_s * 3 && !sys.detector.error
  const busy = sys.workflows_running.length
  return (
    <div className="sb-foot">
      <div className="row"><span className={`live-dot${live ? '' : ' bad'}`} /><span className="sb-label">{live ? `Live · checked ${ago(sys.detector.last!)}` : 'Monitoring paused'}</span></div>
      {!llmOk && <div className="row" title="AI models unreachable"><span className="live-dot bad" /><span className="sb-label">AI models unreachable</span></div>}
      {/* a link, not a label: the incident may be planned or a drill, which the Incidents list hides by default */}
      {busy > 0 && <Link className="row sb-foot-link" title="Open what the AI is investigating"
        to={busy === 1 ? `/incidents/${sys.workflows_running[0]}` : '/incidents?origin=all'}>
        <span className="live-dot warn" /><span className="sb-label">AI investigating {busy} {busy === 1 ? 'incident' : 'incidents'}</span>
      </Link>}
    </div>
  )
}

const NONE: IncidentRow[] = []                // stable empty list: keeps the search memo effective

function GlobalSearch() {
  const [q, setQ] = useState('')
  const [open, setOpen] = useState(false)
  const incidents: IncidentRow[] = useIncidents(open).data ?? NONE    // fetched only while the search is in use
  const [sel, setSel] = useState(0)
  const ref = useRef<HTMLInputElement>(null)
  const nav = useNavigate()

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.key === 'k' && (e.metaKey || e.ctrlKey)) || (e.key === '/' && document.activeElement?.tagName !== 'INPUT' && document.activeElement?.tagName !== 'TEXTAREA')) {
        e.preventDefault(); setOpen(true)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  const results = useMemo(() => {
    const s = q.trim().toLowerCase()
    if (!s) return []
    return incidents.filter(i => `${i.id} ${i.title} ${i.kind} ${i.node} ${i.resource} ${i.status}`.toLowerCase().includes(s)).slice(0, 8)
  }, [q, incidents])

  const close = () => { setQ(''); setOpen(false) }
  const go = (id: string) => { close(); nav(`/incidents/${id}`) }
  useEffect(() => { if (open) ref.current?.focus() }, [open])

  // A sidebar row opens a centred overlay (also / or Ctrl+K); nothing sits on top of the page.
  if (!open) return (
    <button className="sb-link sb-search" title="Search incidents ( / )" onClick={() => setOpen(true)}>
      <Search size={17} /><span className="sb-label">Search</span><kbd className="sb-label">/</kbd>
    </button>
  )
  return (
    <div className="palette-back" onMouseDown={e => { if (e.target === e.currentTarget) close() }}>
    <div className="search palette" role="dialog" aria-label="Search incidents">
      <Search size={16} />
      <input ref={ref} value={q} placeholder="Search incidents by id, title, node, resource…"
        onChange={e => { setQ(e.target.value); setSel(0) }}
        onKeyDown={e => {
          if (e.key === 'ArrowDown') { e.preventDefault(); setSel(s => Math.min(s + 1, results.length - 1)) }
          if (e.key === 'ArrowUp') { e.preventDefault(); setSel(s => Math.max(s - 1, 0)) }
          if (e.key === 'Enter' && results[sel]) go(results[sel].id)
          if (e.key === 'Escape') close()
        }} />
      <kbd>esc</kbd>
      {q && (
        <div className="search-results">
          {results.length === 0 && <div className="empty" style={{ padding: 18 }}>No incidents match “{q}”</div>}
          {results.map((r, i) => (
            <a key={r.id} href={`/incidents/${r.id}`} className={i === sel ? 'sel' : ''} onMouseDown={e => { e.preventDefault(); go(r.id) }}>
              <StatusBadge status={r.status} />
              <span style={{ minWidth: 0, flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{r.title}</span>
              <span className="muted mono hide-sm">{r.id}</span>
            </a>
          ))}
        </div>
      )}
    </div>
    </div>
  )
}

function Popover({ button, children }: { button: (toggle: () => void) => ReactNode; children: (close: () => void) => ReactNode }) {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!open) return
    const h = (e: MouseEvent) => { if (!ref.current?.contains(e.target as Node)) setOpen(false) }
    const k = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false) }
    document.addEventListener('mousedown', h)
    document.addEventListener('keydown', k)
    return () => { document.removeEventListener('mousedown', h); document.removeEventListener('keydown', k) }
  }, [open])
  return <div className="menu" ref={ref}>{button(() => setOpen(o => !o))}{open && <div className="menu-pop">{children(() => setOpen(false))}</div>}</div>
}

export default function Shell({ user, onLogout, children }: { user: User; onLogout: () => void; children: ReactNode }) {
  const summary = useIncidentSummary().data
  const pending = useApprovals('pending').data ?? []
  const sys = useSystem()
  const [sbOpen, setSbOpen] = useState(false)
  const [collapsed, setCollapsed] = useState(() => { try { return localStorage.getItem('aiops.sidebar') === 'collapsed' } catch { return false } })
  useEffect(() => { try { localStorage.setItem('aiops.sidebar', collapsed ? 'collapsed' : 'open') } catch { /* private mode */ } }, [collapsed])
  const loc = useLocation()
  useEffect(() => setSbOpen(false), [loc.pathname])

  const active = summary?.active ?? 0

  return (
    <div className={`shell${collapsed ? ' collapsed' : ''}`}>
      <aside className={`sidebar${sbOpen ? ' open' : ''}`}>
        <div className="sb-top">
          <Link to="/" className="sb-brand"><b>Sovereign <span className="brand-accent">AI Ops</span></b></Link>
          <button className="sb-toggle desktop-only" aria-label={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
            title={collapsed ? 'Expand sidebar' : 'Collapse sidebar'} onClick={() => setCollapsed(c => !c)}>
            {collapsed ? <PanelLeftOpen size={17} /> : <PanelLeftClose size={17} />}
          </button>
        </div>
        <GlobalSearch />
        <NavItem to="/" end icon={<LayoutDashboard size={17} />}>Dashboard</NavItem>
        <div className="sb-section">Operations</div><div className="sb-section-gap" />
        <NavItem to="/incidents" icon={<Siren size={17} />} count={active}>Incidents</NavItem>
        <NavItem to="/approvals" icon={<ClipboardCheck size={17} />} count={pending.length} alert>Approvals</NavItem>
        <NavItem to="/security" icon={<ShieldAlert size={17} />}>Security</NavItem>
        <div className="sb-section">Infrastructure</div><div className="sb-section-gap" />
        <NavItem to="/fleet" icon={<Cpu size={17} />}>Fleet</NavItem>
        <NavItem to="/services" icon={<Container size={17} />}>Services</NavItem>
        <NavItem to="/drift" icon={<GitCompare size={17} />}>Drift</NavItem>
        <NavItem to="/capacity" icon={<Gauge size={17} />}>Capacity</NavItem>
        <NavItem to="/models" icon={<Boxes size={17} />}>Models</NavItem>
        <NavItem to="/history" icon={<BarChart3 size={17} />}>Metric history</NavItem>
        <div className="sb-section">Governance</div><div className="sb-section-gap" />
        <NavItem to="/governance" icon={<ShieldCheck size={17} />}>Governance</NavItem>
        {user.role === 'admin' && <NavItem to="/users" icon={<Users size={17} />}>Users</NavItem>}
        <SystemFoot sys={sys.data ?? null} />
        <div className="sb-account">
          <Popover button={toggle => (
            <button className="sb-user" onClick={toggle} aria-label="Account menu" title={user.username}>
              <span className="avatar">{user.username.slice(0, 2)}</span>
              <span className="sb-label">{user.username}<small>{user.role}</small></span>
            </button>
          )}>
            {close => (
              <>
                <Link to="/account" className="mi" onClick={close}><UserCog size={16} />Account &amp; password</Link>
                <Link to="/governance?tab=policies" className="mi" onClick={close}><BookOpenCheck size={16} />How approvals work</Link>
                <button className="mi" onClick={() => { close(); onLogout() }}><LogOut size={16} />Sign out</button>
              </>
            )}
          </Popover>
          <ThemeSwitcher />
        </div>
      </aside>

      <div className="main">
        <header className="topbar mobile-only">
          <button className="icon-btn" aria-label="Open menu" onClick={() => setSbOpen(true)}><Menu size={18} /></button>
          <Link to="/" className="sb-brand"><b>Sovereign <span className="brand-accent">AI Ops</span></b></Link>
        </header>
        {children}
      </div>
    </div>
  )
}
