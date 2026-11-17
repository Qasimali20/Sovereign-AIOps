/* Charts on Apache ECharts (tree-shaken: line, pie; SVG renderer).
 * Rules kept from the dataviz method: fixed categorical slot order, one y-axis, recessive grid, axis tooltip,
 * legend for >= 2 series, text in ink tokens. Palette validated on the dark surface (blue, magenta, gold,
 * violet): no green, which is reserved for healthy/up states. */
import { useEffect, useMemo, useRef } from 'react'
import * as echarts from 'echarts/core'
import { LineChart, PieChart } from 'echarts/charts'
import { GridComponent, LegendComponent, MarkLineComponent, TooltipComponent, VisualMapComponent } from 'echarts/components'
import { SVGRenderer } from 'echarts/renderers'
import type { EChartsCoreOption } from 'echarts/core'
import { hm, stamp } from '../lib/format'
import { useAppliedTheme } from '../lib/theme'

echarts.use([LineChart, PieChart, GridComponent, LegendComponent, MarkLineComponent, TooltipComponent,
  VisualMapComponent, SVGRenderer])

export const SLOTS = ['var(--s1)', 'var(--s2)', 'var(--s3)', 'var(--s4)']
export type Series = { key: string; name: string; fmt?: (v: number) => string; color?: string }

/** Resolve `var(--token)` to a concrete colour (ECharts needs real values for gradients and opacity). */
export function resolve(c: string | undefined): string {
  if (!c) return '#888888'
  const m = /^var\((--[^)]+)\)$/.exec(c.trim())
  const raw = m ? getComputedStyle(document.documentElement).getPropertyValue(m[1]).trim() : c
  return toHex(raw || '#888888')
}
/** Any CSS colour (hsl with spaces, named, rgb) -> #rrggbb, so ECharts can parse it and add alpha. */
const probe = typeof document !== 'undefined' ? document.createElement('span') : null
function toHex(css: string): string {
  if (/^#[0-9a-f]{6}$/i.test(css)) return css
  if (!probe) return css
  probe.style.color = ''; probe.style.color = css
  if (!probe.isConnected) document.body.appendChild(probe)
  const rgb = getComputedStyle(probe).color.match(/\d+(\.\d+)?/g)
  if (!rgb) return css
  return '#' + rgb.slice(0, 3).map(v => Math.round(Number(v)).toString(16).padStart(2, '0')).join('')
}
const tok = (name: string) => resolve(`var(${name})`)
/** Round up to 1, 2, 2.5 or 5 x 10^n so axis labels stay tidy. */
function nice(v: number): number {
  if (!(v > 0)) return 1
  const p = 10 ** Math.floor(Math.log10(v)), f = v / p
  return (f <= 1 ? 1 : f <= 1.2 ? 1.2 : f <= 2 ? 2 : f <= 4 ? 4 : f <= 6 ? 6 : f <= 8 ? 8 : 10) * p
}

/** Mounts one ECharts instance, resizes with its box, and applies option changes in place. */
export function EChart({ option, height, className, onClick }: {
  option: EChartsCoreOption; height: number; className?: string; onClick?: (params: any) => void
}) {
  const el = useRef<HTMLDivElement>(null)
  const chart = useRef<echarts.ECharts | null>(null)
  const click = useRef(onClick)
  click.current = onClick                          // latest handler without re-binding the chart
  useEffect(() => {
    if (!el.current) return
    chart.current = echarts.init(el.current, undefined, { renderer: 'svg' })
    chart.current.on('click', p => click.current?.(p))
    const ro = new ResizeObserver(() => chart.current?.resize())
    ro.observe(el.current)
    return () => { ro.disconnect(); chart.current?.dispose(); chart.current = null }
  }, [])
  // Callers rebuild options (and formatter closures) on every render, and pages re-render on every poll. Redraw only
  // when the option's content changed: functions are compared by presence, everything else by value.
  const applied = useRef('')
  useEffect(() => {
    const sig = JSON.stringify(option, (_k, v) => (typeof v === 'function' ? 'ƒ' : v))
    if (!chart.current || sig === applied.current) return
    applied.current = sig
    chart.current.setOption(option, { notMerge: true })
  }, [option])
  return <div ref={el} className={`${className ?? ''}${onClick ? ' chart-link' : ''}`} style={{ height, width: '100%' }} />
}

const base = () => ({
  backgroundColor: 'transparent',
  animationDuration: 500,
  textStyle: { fontFamily: getComputedStyle(document.body).fontFamily, color: tok('--ink-2') },
})
const tooltipBase = () => ({
  backgroundColor: tok('--panel'), borderColor: tok('--line-2'), borderWidth: 1, padding: [8, 10],
  textStyle: { color: tok('--ink'), fontSize: 12.5 },
  extraCssText: 'border-radius:10px;box-shadow:0 12px 30px rgba(0,0,0,.6);',
})
const axisLabel = () => ({ color: tok('--muted'), fontSize: 11 })
const dot = (c: string) => `<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${c};margin-right:7px"></span>`

export function TimeChart({ data, series, height = 220, yDomain, yFmt, xFmt = hm, area = false, xDomain, stacked = false, warn, warnLabel, onSelect }: {
  data: Record<string, number | null>[]; series: Series[]; height?: number; onSelect?: (seriesName: string) => void
  yDomain?: [number | string, number | string]; yFmt?: (v: number) => string; xFmt?: (t: number) => string; area?: boolean
  xDomain?: [number, number]; stacked?: boolean; warn?: number | null; warnLabel?: string
}) {
  const theme = useAppliedTheme()
  const option = useMemo<EChartsCoreOption>(() => {
    const colors = series.map((s, i) => resolve(s.color ?? SLOTS[i % SLOTS.length]))
    const sparse = (k: string) => data.length > 0 && data.filter(d => d[k] != null).length < data.length * 0.6
    const fill = area || stacked
    // y-axis top: fixed (e.g. 100%) or the data peak (stacked: the column total) rounded to a tidy value, then four
    // equal steps, so the axis never shows an odd extra label such as 90 under 100
    const peak = Math.max(0, ...data.map(d => stacked
      ? series.reduce((a, s) => a + (d[s.key] ?? 0), 0)
      : Math.max(0, ...series.map(s => d[s.key] ?? 0))))
    const yMax = typeof yDomain?.[1] === 'number' ? yDomain[1] as number : nice(Math.max(peak * 1.05, warn != null ? warn * 1.1 : 0) || 1)
    return {
      ...base(),
      color: colors,
      grid: { left: 6, right: 14, top: series.length > 1 ? 34 : 12, bottom: 4, containLabel: true },
      legend: series.length > 1 ? {
        type: 'scroll', top: 0, left: 0, right: 0, icon: 'roundRect', itemWidth: 12, itemHeight: 4, itemGap: 16,   // one row: never over the plot
        textStyle: { color: tok('--ink-2'), fontSize: 12 }, inactiveColor: tok('--dim'),
        pageIconSize: 9, pageIconColor: tok('--ink-2'), pageIconInactiveColor: tok('--dim'), pageTextStyle: { color: tok('--muted'), fontSize: 11 },
      } : undefined,
      tooltip: {
        ...tooltipBase(), trigger: 'axis',
        axisPointer: { type: 'line', lineStyle: { color: tok('--line-2') } },
        formatter: (ps: any[]) => {
          const rows = ps.filter(p => p.value?.[1] != null)
          const total = stacked && rows.length > 1 ? rows.reduce((a, p) => a + p.value[1], 0) : null
          const f = (i: number, v: number) => (series[i]?.fmt ?? yFmt ?? String)(v)
          return `<div style="color:${tok('--muted')};margin-bottom:4px">${stamp(ps[0].value[0] / 1000)}</div>`
            + (total != null ? `<div style="display:flex;justify-content:space-between;gap:18px;font-weight:700;border-bottom:1px solid ${tok('--line')};padding-bottom:3px;margin-bottom:3px"><span>Total</span><span>${f(0, total)}</span></div>` : '')
            + rows.map(p => `<div style="display:flex;justify-content:space-between;gap:18px"><span>${dot(p.color)}${p.seriesName}</span><b>${f(p.seriesIndex, p.value[1])}</b></div>`).join('')
        },
      },
      xAxis: {
        type: 'time', min: xDomain ? xDomain[0] * 1000 : 'dataMin', max: xDomain ? xDomain[1] * 1000 : 'dataMax',
        axisLine: { lineStyle: { color: tok('--line') } }, axisTick: { show: false }, splitLine: { show: false },
        axisLabel: { ...axisLabel(), hideOverlap: true, formatter: (v: number) => xFmt(v / 1000) },
      },
      yAxis: {
        type: 'value', min: yDomain?.[0] === 'auto' ? undefined : (yDomain?.[0] as number ?? 0),
        // fixed max (e.g. 100%) -> four even steps; otherwise round up to a tidy value that still shows the threshold
        max: yMax, interval: yMax / 4,
        splitNumber: 4, axisLabel: { ...axisLabel(), formatter: (v: number) => (yFmt ? yFmt(v) : String(v)) },
        splitLine: { lineStyle: { color: tok('--line'), type: 'dashed' } },
      },
      series: series.map((s, i) => ({
        name: s.name, type: 'line', smooth: 0.25, connectNulls: false, triggerLineEvent: !!onSelect,
        stack: stacked ? 'total' : undefined,
        showSymbol: sparse(s.key), symbol: 'circle', symbolSize: 5,
        lineStyle: { width: stacked ? 1.5 : 2.5, color: colors[i], shadowColor: stacked ? 'transparent' : colors[i] + '55', shadowBlur: stacked ? 0 : 6, shadowOffsetY: 2 },
        emphasis: { focus: 'series' },
        // every series gets a soft gradient wash; stacked series a stronger one so the bands read as areas
        areaStyle: {
          opacity: 1,
          color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
            { offset: 0, color: colors[i] + (stacked ? 'e6' : fill ? '66' : series.length > 2 ? '22' : '38') },
            { offset: 1, color: colors[i] + (stacked ? '80' : '00') }]),
        },
        data: data.map(d => [(d.t as number) * 1000, d[s.key]]),
        markLine: i === 0 && warn != null ? {
          silent: true, symbol: 'none',
          lineStyle: { color: tok('--crit'), type: [5, 4], width: 1, opacity: .8 },
          label: { formatter: `${yFmt ? yFmt(warn) : warn}${warnLabel ? ` · ${warnLabel}` : ''}`, position: 'insideEndTop', color: tok('--crit-ink'), fontSize: 11 },
          data: [{ yAxis: warn }],
        } : undefined,
      })),
    }
  }, [data, series, yDomain, yFmt, xFmt, area, xDomain, stacked, warn, warnLabel, theme, onSelect])
  return <EChart option={option} height={height} onClick={onSelect && (p => p.seriesName && onSelect(p.seriesName))} />
}

/** Small trend for cards: no axes, 0-100 scale, optional soft fill. */
export function MiniLines({ data, lines, height = 72, domain = [0, 100] }: {
  data: Record<string, number | null>[]; lines: { k: string; color: string; fill?: boolean }[]; height?: number; domain?: [number, number]
}) {
  const theme = useAppliedTheme()
  const option = useMemo<EChartsCoreOption>(() => ({
    ...base(), animation: false,
    grid: { left: 0, right: 0, top: 4, bottom: 0 },
    xAxis: { type: 'time', show: false }, yAxis: { type: 'value', show: false, min: domain[0], max: domain[1] },
    series: lines.map(l => {
      const c = resolve(l.color)
      return {
        type: 'line', smooth: 0.3, showSymbol: false, lineStyle: { width: l.fill ? 2 : 1.5, color: c },
        areaStyle: l.fill ? { color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [{ offset: 0, color: c + '55' }, { offset: 1, color: c + '00' }]) } : undefined,
        data: data.map(d => [(d.t as number) * 1000, d[l.k]]),
      }
    }),
  }), [data, lines, domain, theme])
  return <EChart option={option} height={height} />
}

/** Ring chart for a composition at one moment: total in the middle, labelled legend with values beside it. */
export function Donut({ items, height = 220, centerLabel = 'total', fmt = (v: number) => String(Math.round(v)), onSelect }: {
  items: { name: string; value: number; color: string; key?: string }[]; height?: number; centerLabel?: string; fmt?: (v: number) => string
  onSelect?: (key: string) => void
}) {
  const total = items.reduce((a, i) => a + i.value, 0)
  const theme = useAppliedTheme()
  const option = useMemo<EChartsCoreOption>(() => ({
    ...base(),
    tooltip: { ...tooltipBase(), trigger: 'item', formatter: (p: any) => `${dot(p.color)}${p.name}<b style="margin-left:14px">${fmt(p.value)}</b> <span style="color:${tok('--muted')}">${p.percent}%</span>` },
    series: [{
      type: 'pie', radius: ['60%', '88%'], center: ['50%', '50%'], padAngle: 3, minAngle: 5,
      itemStyle: { borderRadius: 8, borderColor: 'transparent', shadowBlur: 10, shadowColor: 'rgba(0,0,0,.12)' },
      label: { show: false }, labelLine: { show: false },
      emphasis: { scale: true, scaleSize: 4 },
      data: items.filter(i => i.value > 0).map(i => ({ name: i.name, value: i.value, key: i.key ?? i.name, itemStyle: { color: resolve(i.color) } })),
    }],
  }), [items, fmt, theme])
  return (
    <div className="donut">
      <div className="donut-chart" style={{ height }}>
        <EChart option={option} height={height} onClick={onSelect && (p => p.data?.key && onSelect(p.data.key))} />
        <div className="donut-c"><b>{fmt(total)}</b><span>{centerLabel}</span></div>
      </div>
      <ul className="donut-legend">
        {items.map(i => (
          <li key={i.name} className={onSelect ? 'clickable' : undefined} {...(onSelect ? { role: 'button', tabIndex: 0,
              onClick: () => onSelect(i.key ?? i.name), onKeyDown: (e: React.KeyboardEvent) => { if (e.key === 'Enter') onSelect(i.key ?? i.name) } } : {})}>
            <i style={{ background: i.color }} /><span>{i.name}</span><b>{fmt(i.value)}</b>
            <small>{total ? `${Math.round((100 * i.value) / total)}%` : ''}</small></li>
        ))}
      </ul>
    </div>
  )
}
