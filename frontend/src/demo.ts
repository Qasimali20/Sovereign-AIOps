/* Offline demo: answers every API call from a recorded snapshot embedded in the page (built by
 * `npm run build:demo`, see scripts/build-demo.mjs). Read-only: any action is refused with a clear message.
 * The clock is set to the moment the snapshot was taken, so "3 min ago" reads as it did then. */
import { ApiError } from './lib/api'

type Snapshot = { taken: number; responses: Record<string, unknown> }
let snap: Snapshot

export const taken = () => snap.taken

/** Decode the embedded snapshot (base64 gzip in <script id="demo-snapshot">) and freeze the clock to it. */
export async function init(): Promise<void> {
  const b64 = document.getElementById('demo-snapshot')!.textContent!.trim()
  const bytes = Uint8Array.from(atob(b64), c => c.charCodeAt(0))
  const text = await new Response(new Blob([bytes]).stream().pipeThrough(new DecompressionStream('gzip'))).text()
  snap = JSON.parse(text)
  const offset = snap.taken * 1000 - Date.now()
  const Real = Date
  class DemoDate extends Real {
    constructor(...a: unknown[]) {
      if (a.length) super(...(a as [string]))
      else super(Real.now() + offset)
    }
    static now() { return Real.now() + offset }
  }
  globalThis.Date = DemoDate as DateConstructor
}

const clone = <T,>(v: T): T => structuredClone(v)
const get = (key: string) => snap.responses[key]

/** Same path with one query parameter replaced (used to fall back to a recorded range). */
function withParam(path: string, k: string, v: string): string {
  const [p, qs = ''] = path.split('?')
  const sp = new URLSearchParams(qs)
  sp.set(k, v)
  return `${p}?${[...sp].map(([a, b]) => `${a}=${encodeURIComponent(b)}`).join('&')}`
}

function resolve(path: string): unknown {
  if (path in snap.responses) return clone(get(path))
  const [base, qs = ''] = path.split('?')
  const sp = new URLSearchParams(qs)

  if (base === '/audit') {                                   // filters run server-side live: apply them here
    const all = get('/audit?limit=1000&actor=&event=&incidents_only=false') as { actor: string; event: string }[]
    const actor = sp.get('actor'), event = sp.get('event')
    return clone(all.filter(e => (!actor || e.actor === actor) && (!event || e.event === event)))
  }
  if (base === '/llm/requests') {                             // filters run server-side live: apply them here
    const all = get(`/llm/requests?range=${sp.get('range')}&model=&client=&outcome=&limit=200`) as
      { model: string | null; client: string | null; outcome: string }[] | undefined
    if (all) {
      const model = sp.get('model'), client = sp.get('client'), outcome = sp.get('outcome')
      return clone(all.filter(r => (!model || r.model === model) && (!client || r.client === client)
        && (!outcome || (outcome === 'errors' ? r.outcome !== 'ok' : r.outcome === outcome))))
    }
  }
  if (sp.has('range'))                                       // an unrecorded range: show the nearest recorded one
    for (const r of ['6h', '1h', '24h', '15m']) {
      const alt = withParam(path, 'range', r)
      if (alt in snap.responses) return clone(get(alt))
    }
  throw new ApiError(404, 'Not included in this demo snapshot.')
}

export async function request<T>(path: string, body?: unknown): Promise<T> {
  if (body === undefined) return resolve(path) as T
  if (path === '/login') return clone(get('/me')) as T
  if (path === '/logout') return { ok: true } as T
  throw new ApiError(403, 'This is a read-only demo snapshot: actions are disabled.')
}
