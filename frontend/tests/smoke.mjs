// Browser smoke test: sign in, open every page (and one of each detail page), fail on any uncaught error,
// console error, failed API call, error-boundary screen or page that renders no heading.
//
//   SMOKE_PASS=… npm run test:smoke          (SMOKE_URL defaults to http://127.0.0.1:5170, SMOKE_USER to admin)
import { chromium } from 'playwright'

const BASE = process.env.SMOKE_URL ?? 'http://127.0.0.1:5170'
const USER = process.env.SMOKE_USER ?? 'admin'
const PASS = process.env.SMOKE_PASS
if (!PASS) { console.error('set SMOKE_PASS'); process.exit(2) }

const PAGES = ['/', '/incidents', '/approvals', '/security', '/fleet', '/services', '/drift', '/models', '/models?tab=requests', '/models?tab=evals', '/models?tab=registry',
  '/governance?tab=agent', '/governance?tab=deployments', '/history',
  '/governance?tab=audit', '/governance?tab=policies', '/users', '/account']
// detail pages are reached through the UI, so the links themselves are tested too
const DETAIL = [['/incidents?scope=all&origin=all', 'a.row-link[href^="/incidents/INC-"]'], ['/models', 'a[href^="/models/"]'],
  ['/fleet', 'a[href^="/fleet/Node-"]'], ['/governance?tab=agent', '/governance?tab=deployments', 'a.row-link[href^="/agent/INC-"]']]

const browser = await chromium.launch()
const failures = []
for (const [scheme, viewport] of [['dark', { width: 1440, height: 900 }], ['light', { width: 390, height: 844 }]]) {
  const page = await browser.newPage({ viewport, colorScheme: scheme })
  let at = 'login'
  const fail = msg => failures.push(`[${scheme} ${viewport.width}px] ${at}: ${msg}`)
  page.on('pageerror', e => fail(`uncaught ${e.message}`))
  page.on('console', m => { if (m.type() === 'error' && at !== 'login') fail(`console ${m.text()}`) })   // the pre-login session probe is a 401 by design
  page.on('response', r => { if (r.url().includes('/api/') && r.status() >= 500) fail(`HTTP ${r.status()} ${new URL(r.url()).pathname}`) })

  await page.goto(BASE + '/login')
  await page.fill('input:not([type=password])', USER)
  await page.fill('input[type=password]', PASS)
  await page.keyboard.press('Enter')
  await page.waitForURL(u => !u.pathname.startsWith('/login'), { timeout: 10_000 })

  const check = async path => {
    at = path
    await page.goto(BASE + path)
    await page.waitForLoadState('networkidle', { timeout: 15_000 }).catch(() => {})   // live pages poll: idle may never come
    if (await page.locator('.callout.crit button', { hasText: 'Try again' }).count()) fail('error boundary shown')
    if (!(await page.locator('main h1').count())) fail('no page heading')
    if (await page.locator('main h1', { hasText: 'Page not found' }).count()) fail('route not found')
  }
  for (const p of PAGES) await check(p)
  for (const [list, link] of DETAIL) {
    await check(list)
    const href = await page.locator(link).first().getAttribute('href').catch(() => null)
    if (href) await check(href); else fail(`no detail link (${link})`)
  }
  await page.close()
}
await browser.close()

if (failures.length) { console.error(`smoke: ${failures.length} failure(s)\n  ` + failures.join('\n  ')); process.exit(1) }
console.log(`smoke: ${PAGES.length + DETAIL.length * 2} pages × 2 viewports, no errors`)
