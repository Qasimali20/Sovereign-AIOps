import { useMemo } from 'react'
import { useRange } from '../lib/useRange'
import { useMetricsBoard } from '../lib/queries'
import type { BoardPanel } from '../lib/api'
import { Card, Empty, PageHead, Segmented, Skeleton } from './ui'
import { Tile } from './Tile'
import { dateTime } from '../lib/format'
import { Panel, RANGES, fmt, type Range } from './MetricPanel'

// a chart whose metric has no series at all in the range: one line under its section instead of an empty card
const silent = (p: BoardPanel) => !p.error && !p.series.length

/** One Prometheus board: KPI row, then its panels grouped by section. The range lives in the URL (?range=).
 *  `page` makes the board the whole page: its title is the page heading, with the range picker beside it. */
export const RangePicker = ({ range, onChange }: { range: Range; onChange: (r: Range) => void }) =>
  <Segmented value={range} onChange={onChange} options={RANGES} />

// tile colours in order, as on the Spark Monitor's metrics strip
const TILE_COLOURS = ['var(--m-gpu)', 'var(--warn)', 'var(--m-mem)', 'var(--s3)']

/** The board's KPI row. */
function BoardStats({ board, range }: { board: 'models' | 'fleet'; range: Range }) {
  const { data } = useMetricsBoard(board, range)
  const stats = data?.board === board ? data.stats : null
  return (
    <div className="ftiles">
      {(stats ?? Array.from({ length: 4 }, () => null)).map((s, i) => s
        ? <Tile key={s.title} i={i} label={s.title} title={s.desc || undefined} color={TILE_COLOURS[i % TILE_COLOURS.length]}
            value={s.value ?? null} fmt={v => fmt(s.unit, v)} pct={s.unit === 'percent' ? s.value : undefined} foot="now" />
        : <div key={i} className="ftile"><Skeleton h={50} /></div>)}
    </div>
  )
}

/** `range` given: the page owns the range (and its picker); the board just follows it. `stats={false}`: no KPI row
 *  (the page shows its own). */
export default function MetricBoard({ board, title, page, range: owned, stats = true }: {
  board: 'models' | 'fleet'; title?: string; page?: string; range?: Range; stats?: boolean
}) {
  const [own, pick] = useRange()
  const range = owned ?? own
  const { data, isError, isFetching } = useMetricsBoard(board, range)
  const ready = data?.board === board && data.range === range
  const sections = useMemo(() => {
    const m = new Map<string, BoardPanel[]>()
    for (const p of ready ? data!.panels : []) m.set(p.section, [...(m.get(p.section) ?? []), p])
    return [...m.entries()]
  }, [data, ready])
  // Prometheus started collecting recently: say so when the data covers only part of the chosen range
  const firstPoint = useMemo(() => {
    const ts = (ready ? data!.panels : []).flatMap(p => p.points.length ? [p.points[0][0] as number] : [])
    return ts.length ? Math.min(...ts) : null
  }, [data, ready])
  const partial = ready && firstPoint != null && firstPoint - data!.start > 0.1 * (data!.end - data!.start)

  const controls = (
    <div className="row" style={{ gap: 10 }}>
      {isFetching && ready && <span className="small muted">Updating…</span>}
      {!owned && <RangePicker range={range} onChange={pick} />}
    </div>
  )

  return (
    <>
      {page ? <PageHead title={page} actions={controls} /> : title && (
        <div className="row" style={{ justifyContent: 'space-between', flexWrap: 'wrap', gap: 12 }}>
          <h2 className="section-title" style={{ margin: 0 }}>{title}</h2>{controls}
        </div>
      )}
      {isError && !data ? <Card><Empty>Prometheus on Node-3 is not answering.</Empty></Card> : (
        <>
          {stats && <BoardStats board={board} range={range} />}
          {partial && <div className="small ink2">Data from {dateTime(firstPoint)}</div>}
          {ready ? sections.map(([name, panels]) => (
            <section key={name} className="stack" style={{ gap: 12 }}>
              {name && <h2 className="section-title" style={{ margin: '8px 0 0', fontSize: 15 }}>{name}</h2>}
              <div className="grid g-2">{panels.filter(p => !silent(p)).map(p => <Panel key={p.id} p={p} range={range} span={[data!.start, data!.end]} />)}</div>
              {panels.some(silent) && <div className="small muted">
                No data in this range: {panels.filter(silent).map(p => p.absent ? `${p.title} (${p.absent})` : p.title).join(' · ')}
              </div>}
            </section>
          )) : <div className="grid g-2">{Array.from({ length: 4 }, (_, i) => <Card key={i}><Skeleton h={220} /></Card>)}</div>}
        </>
      )}
    </>
  )
}
