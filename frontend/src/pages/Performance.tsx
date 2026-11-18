import { useState } from 'react'
import { Gauge, Play } from 'lucide-react'
import { canApprove, type Bottleneck, type PerfResult, type User } from '../lib/api'
import { useBottlenecks, usePerf, useRunPerf } from '../lib/queries'
import { Badge, Card, Empty, Page, PageHead, Segmented, Skeleton, useToast } from '../components/ui'
import { ago, dateTime } from '../lib/format'

const SCEN: Record<string, string> = { short: 'Short prompt, 128 tokens out', long_prompt: 'Long prompt (~2k tokens), 16 out' }
const f1 = (v: number | null | undefined, d = 1) => v == null ? '–' : v.toFixed(d)
const pct = (v: number | null | undefined) => v == null ? '–' : `${Math.round(v * 100)}%`

/** Value against the server's own usual level: how far off, never colour alone. */
function VsUsual({ now, usual, unit, lowerIsBetter, d = 1 }: { now: number | null; usual: number | null; unit: string; lowerIsBetter?: boolean; d?: number }) {
  if (now == null) return <span className="muted">–</span>
  const ch = usual ? (now - usual) / usual : null
  const bad = ch != null && (lowerIsBetter ? ch > 0.5 : ch < -0.25)
  return (
    <span title={usual ? `usual ${usual.toFixed(d)} ${unit}` : 'no baseline yet'}>
      {now.toFixed(d)} <span className="muted small">{unit}</span>
      {ch != null && Math.abs(ch) >= 0.05 && <span className={`small ${bad ? 'crit-ink' : 'muted'}`}> {ch > 0 ? '+' : ''}{Math.round(ch * 100)}%</span>}
    </span>
  )
}

function Benchmarks({ user }: { user: User }) {
  const { data } = usePerf()
  const run = useRunPerf()
  const toast = useToast()
  const rows = data?.results ?? []
  return (
    <>
      {data && data.regressions.length > 0 && (
        <Card title="Slower than usual">
          <ul className="items">{data.regressions.map(r => (
            <li key={`${r.node}/${r.model}/${r.scenario}`}>
              <Badge tone="warn">regression</Badge> <b>{r.model}</b> <span className="muted small">{r.node} · {SCEN[r.scenario] ?? r.scenario}</span>
              <div className="small">{r.findings.join('; ')}{r.version_changed && ' · the model or engine version changed since the baseline'}</div>
            </li>
          ))}</ul>
        </Card>
      )}
      <Card title="Benchmarks" hint={data ? `${data.last_run ? `last run ${ago(data.last_run)}` : 'not run yet'} · every ${data.interval_s / 3600} h · `
        + `quiet servers only (GPU under ${data.guard.max_temp_c} °C and ${data.guard.max_gpu_util}% busy, no requests in flight)` : undefined}
        right={canApprove(user) && <button className="btn btn-sm" disabled={run.isPending || data?.running} onClick={() =>
          run.mutate(undefined, { onSuccess: () => toast('good', 'benchmark started'), onError: e => toast('crit', String(e)) })}>
          <Play size={14} /> {data?.running ? 'Running…' : 'Benchmark now'}</button>}
        bodyClass="card-body tight">
        {data === undefined ? <div className="card-body"><Skeleton h={160} /></div> : rows.length === 0 ? (
          <Empty icon={<Gauge size={28} />}>No benchmark has run yet.</Empty>
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead><tr><th>Server</th><th>Scenario</th><th className="num">First token</th><th className="num">Output speed</th>
                <th className="num hide-sm">Prompt speed</th><th className="num hide-sm">Runs</th><th className="hide-sm">Measured</th></tr></thead>
              <tbody>{rows.map((r: PerfResult) => (
                <tr key={`${r.node}/${r.model}/${r.scenario}`}>
                  <td><b>{r.model}</b><div className="sub">{r.node}:{r.port}{r.engine_version ? ` · vLLM ${r.engine_version}` : ''}</div></td>
                  <td className="small">{SCEN[r.scenario] ?? r.scenario}</td>
                  {r.skipped || r.error ? (
                    <td colSpan={3} className="small muted">{r.skipped ? `skipped: ${r.skipped}` : `failed: ${r.error}`}</td>
                  ) : (<>
                    <td className="num nowrap"><VsUsual now={r.ttft_s} usual={r.baseline.ttft_s} unit="s" lowerIsBetter d={2} /></td>
                    <td className="num nowrap">{r.scenario === 'short' ? <VsUsual now={r.decode_tps} usual={r.baseline.decode_tps} unit="tok/s" /> : <span className="muted">–</span>}</td>
                    <td className="num nowrap hide-sm">{r.scenario === 'long_prompt' ? <VsUsual now={r.prefill_tps} usual={r.baseline.prefill_tps} unit="tok/s" d={0} /> : <span className="muted">–</span>}</td>
                  </>)}
                  <td className="num hide-sm">{r.runs}</td>
                  <td className="muted nowrap hide-sm" title={dateTime(r.t)}>{ago(r.t)}</td>
                </tr>
              ))}</tbody>
            </table>
          </div>
        )}
      </Card>
    </>
  )
}

function Bottlenecks() {
  const [w, setW] = useState<'1h' | '6h' | '24h' | '7d'>('24h')
  const { data, error } = useBottlenecks(w)
  return (
    <Card title="Where the time goes" hint="production traffic, from each server's own metrics"
      right={<Segmented value={w} onChange={setW} options={(['1h', '6h', '24h', '7d'] as const).map(k => ({ key: k, label: k }))} />}>
      {error ? <div className="muted">{String(error)}</div> : data === undefined ? <Skeleton h={160} /> : data.length === 0 ? (
        <Empty icon={<Gauge size={28} />}>No chat or vision server reported metrics in this window.</Empty>
      ) : (
        <ul className="items">{data.map((b: Bottleneck) => {
          const s = b.stats, e2e = s.e2e_s || null
          return (
            <li key={`${b.node}/${b.model}`}>
              <div className="row" style={{ justifyContent: 'space-between', gap: 8, flexWrap: 'wrap' }}>
                <span><b>{b.model}</b> <span className="muted small">{b.node}</span></span>
                <span className="small muted">{f1(s.requests, 0)} requests · queued {pct(e2e ? (s.queue_s ?? 0) / e2e : null)} · prefill {pct(e2e ? (s.prefill_s ?? 0) / e2e : null)}
                  {' '}· decode {pct(e2e ? (s.decode_s ?? 0) / e2e : null)} · first token {f1(s.ttft_s, 2)} s · {s.itl_s ? (1 / s.itl_s).toFixed(0) : '–'} tok/s per request
                  {' '}· KV peak {pct(s.kv_max)}</span>
              </div>
              {b.findings.map(f => (
                <div key={f.rule} style={{ marginTop: 6 }}>
                  <Badge tone={f.severity === 'warning' ? 'warn' : 'neutral'}>{f.title}</Badge> <span className="small">{f.detail}</span>
                  {f.action && <div className="small muted">{f.action}</div>}
                </div>
              ))}
            </li>
          )
        })}</ul>
      )}
    </Card>
  )
}

/** LLMOps: idle benchmarks with regression detection, and where production requests spend their time. */
export default function Performance({ user, embedded = false }: { user: User; embedded?: boolean }) {
  return (
    <Page embedded={embedded}>
      <PageHead embedded={embedded} title="Performance" />
      <Benchmarks user={user} />
      <Bottlenecks />
    </Page>
  )
}
