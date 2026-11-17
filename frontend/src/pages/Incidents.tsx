import { ORIGIN_LABEL, categoryOf, isFinal } from '../lib/status'
import { useMemo, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { ArrowDown, ArrowUp, RefreshCw, Siren, X } from 'lucide-react'
import type { IncidentRow } from '../lib/api'
import { useIncidents, useScan } from '../lib/queries'
import { Card, Empty, PageHead, Segmented, StatusBadge, useToast } from '../components/ui'
import { ago, dateTime, duration, label } from '../lib/format'

type SortKey = 'created' | 'status' | 'kind' | 'node' | 'updated'
const PAGE = 25

export default function Incidents() {
  const nav = useNavigate()
  const toast = useToast()
  const { data } = useIncidents()
  const scanM = useScan()
  const [params, setParams] = useSearchParams()
  const scope = (params.get('scope') ?? 'active') as 'active' | 'approval' | 'all'
  const [q, setQ] = useState('')
  const [kind, setKind] = useState('')
  const [node, setNode] = useState('')
  const [sort, setSort] = useState<{ k: SortKey; desc: boolean }>({ k: 'created', desc: true })
  const [page, setPage] = useState(0)
  const origin = (params.get('origin') ?? 'real') as 'real' | 'other' | 'all'
  const status = params.get('status') ?? '', category = params.get('category') ?? ''
  const clearFilter = () => setParams(p => { const n = new URLSearchParams(p); n.delete('status'); n.delete('category'); return n }, { replace: true })

  const everything = data ?? []
  const rows = everything.filter(r => (!status || r.status === status) && (!category || categoryOf(r.kind) === category)).filter(r => origin === 'all' || (origin === 'real' ? (r.origin ?? 'real') === 'real' : (r.origin ?? 'real') !== 'real'))
  const setOrigin = (o: string) => setParams(p => { const n = new URLSearchParams(p); if (o === 'real') n.delete('origin'); else n.set('origin', o); return n }, { replace: true })
  const hiddenActive = everything.filter(r => !isFinal(r.status) && (r.origin ?? 'real') !== 'real').length
  const kinds = [...new Set(rows.map(r => r.kind))].sort()
  const nodes = [...new Set(rows.map(r => r.node))].sort()

  const filtered = useMemo(() => {
    const s = q.trim().toLowerCase()
    const out = rows.filter(r =>
      (scope === 'all' || (scope === 'active' ? !isFinal(r.status) : r.status === 'awaiting_approval'))
      && (!kind || r.kind === kind) && (!node || r.node === node)
      && (!s || `${r.id} ${r.title} ${r.resource}`.toLowerCase().includes(s)))
    out.sort((a, b) => {
      const va = a[sort.k] as string | number, vb = b[sort.k] as string | number
      return (va < vb ? -1 : va > vb ? 1 : 0) * (sort.desc ? -1 : 1)
    })
    return out
  }, [rows, scope, kind, node, q, sort])
  const pages = Math.max(1, Math.ceil(filtered.length / PAGE))
  const shown = filtered.slice(page * PAGE, page * PAGE + PAGE)

  const th = (k: SortKey, text: string, cls = '') => (
    <th className={`sortable ${cls}`} aria-sort={sort.k === k ? (sort.desc ? 'descending' : 'ascending') : 'none'}>
      <button type="button" className="th-sort" onClick={() => setSort(s => ({ k, desc: s.k === k ? !s.desc : true }))}>
        {text}{sort.k === k && (sort.desc ? <ArrowDown size={12} /> : <ArrowUp size={12} />)}
      </button>
    </th>
  )

  const scan = () => scanM.mutate(undefined, {
    onSuccess: r => toast(r.opened.length ? 'good' : 'info', r.opened.length ? `Opened ${r.opened.length} new incident(s)` : 'Scan complete: no new incidents'),
    onError: e => toast('crit', e.message),
  })

  const count = (sc: string) => rows.filter(r => sc === 'all' || (sc === 'active' ? !isFinal(r.status) : r.status === 'awaiting_approval')).length

  return (
    <main className="content">
      <PageHead title="Incidents"
        actions={<button className="btn" onClick={scan} disabled={scanM.isPending}><RefreshCw size={15} className={scanM.isPending ? 'spin' : ''} />Scan fleet now</button>} />
      <Card bodyClass="card-body tight">
        <div className="filters">
          <Segmented value={origin} onChange={o => { setOrigin(o); setPage(0) }}
            options={[{ key: 'real', label: 'Real' }, { key: 'other', label: 'Drills & planned' }, { key: 'all', label: 'All' }]} />
          <Segmented value={scope} onChange={k => { setParams(p => { const n = new URLSearchParams(p); if (k === 'active') n.delete('scope'); else n.set('scope', k); return n }); setPage(0) }}
            options={[{ key: 'active', label: `Active (${count('active')})` }, { key: 'approval', label: `Needs approval (${count('approval')})` }, { key: 'all', label: `All (${rows.length})` }]} />
          <input className="search-in" aria-label="Filter incidents" placeholder="Filter by id, title or resource" value={q} onChange={e => { setQ(e.target.value); setPage(0) }} />
          <select aria-label="Kind" value={kind} onChange={e => { setKind(e.target.value); setPage(0) }}>
            <option value="">All kinds</option>{kinds.map(k => <option key={k} value={k}>{label(k)}</option>)}
          </select>
          {(status || category) && <button className="chip chip-btn filter-chip" onClick={clearFilter} title="Clear this filter">
            {category || label(status)} <X size={12} /></button>}
          <select aria-label="Node" value={node} onChange={e => { setNode(e.target.value); setPage(0) }}>
            <option value="">All nodes</option>{nodes.map(n => <option key={n}>{n}</option>)}
          </select>
        </div>
        {data === undefined ? <Empty>Loading…</Empty> : filtered.length === 0 ? (
          <Empty icon={<Siren size={28} />}>
            {scope === 'active' ? `No active ${origin === 'real' ? 'real ' : ''}incidents. The fleet looks healthy.` : 'No incidents match these filters.'}
            {/* say so when the Real filter hides active drills or planned work, so the sidebar count never looks lost */}
            {scope === 'active' && origin === 'real' && hiddenActive > 0 && <div className="small" style={{ marginTop: 6 }}>
              {hiddenActive} drill or planned {hiddenActive === 1 ? 'incident is' : 'incidents are'} active.{' '}
              <button className="link-btn" onClick={() => setOrigin('other')}>Show {hiddenActive === 1 ? 'it' : 'them'}</button>
            </div>}
          </Empty>
        ) : (
          <div className="table-wrap">
            <table className="table click">
              <thead>
                <tr>
                  <th>Incident</th>{th('status', 'Status')}{th('kind', 'Kind', 'hide-sm')}{th('node', 'Node')}
                  {th('created', 'Opened', 'hide-sm')}<th className="hide-sm">Duration</th>
                </tr>
              </thead>
              <tbody>
                {shown.map((r: IncidentRow) => (
                  <tr key={r.id} onClick={() => nav(`/incidents/${r.id}`)}>
                    <td><Link className="primary row-link" to={`/incidents/${r.id}`} onClick={e => e.stopPropagation()}>{r.title}</Link><div className="sub">{r.id} · {r.resource}
                      {(r.occurrences ?? 1) > 1 && <span className="chip chip-recur" title="the same problem came back after clearing; one incident tracks every occurrence">recurred {r.occurrences}×</span>}
                      {r.origin && r.origin !== 'real' && <span className={`chip chip-origin ${r.origin}`}>{ORIGIN_LABEL[r.origin]}</span>}</div></td>
                    <td className="nowrap"><StatusBadge status={r.status} /></td>
                    <td className="ink2 hide-sm">{label(r.kind)}</td>
                    <td className="nowrap">{r.node}</td>
                    <td className="muted nowrap hide-sm" title={dateTime(r.created)}>{ago(r.created)}</td>
                    <td className="muted nowrap hide-sm">{isFinal(r.status) ? duration(r.updated - r.created) : duration(Date.now() / 1000 - r.created) + ' so far'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {filtered.length > PAGE && (
          <div className="pager">
            <span>{page * PAGE + 1}–{Math.min(filtered.length, (page + 1) * PAGE)} of {filtered.length}</span>
            <span className="spacer" />
            <button className="btn btn-sm" disabled={page === 0} onClick={() => setPage(p => p - 1)}>Previous</button>
            <button className="btn btn-sm" disabled={page >= pages - 1} onClick={() => setPage(p => p + 1)}>Next</button>
          </div>
        )}
      </Card>
    </main>
  )
}
