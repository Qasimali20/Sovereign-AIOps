// Offline demo: the real console, answering from a recorded snapshot, as ONE self-contained HTML file.
//
//   ../.venv/bin/python ../scripts/demo_snapshot.py   # record demo/snapshot.json from the live platform
//   npm run build:demo                                 # -> dist-demo/sovereign-ai-ops-demo.html
//
// Everything is inlined (scripts, styles, fonts, data), so the file opens from disk with no server and no network.
import { mkdirSync, readFileSync, writeFileSync, rmSync } from 'node:fs'
import { gzipSync } from 'node:zlib'
import { build } from 'vite'
import react from '@vitejs/plugin-react'

const OUT = 'dist-demo'
const snapshot = readFileSync('demo/snapshot.json')
process.env.VITE_DEMO = '1'

rmSync(OUT, { recursive: true, force: true })
// Fonts: only the Latin variable faces the UI uses (every script and italic subset would add ~6 MB of data URIs).
const latinFontsOnly = {
  name: 'latin-fonts-only',
  enforce: 'pre',                         // see the raw CSS, before font URLs become asset placeholders
  transform(code, id) {
    if (!/@fontsource-variable\/.+\.css$/.test(id)) return
    return code.replace(/@font-face\s*{[^}]*}/g, f => /-latin-wght-normal\.woff2/.test(f) ? f : '')
  },
}

await build({
  configFile: false,
  plugins: [react(), latinFontsOnly],
  base: './',
  logLevel: 'warn',
  build: {
    outDir: OUT, emptyOutDir: true, assetsInlineLimit: Number.MAX_SAFE_INTEGER, cssCodeSplit: false,
    chunkSizeWarningLimit: Number.MAX_SAFE_INTEGER, modulePreload: false,
    rollupOptions: { output: { codeSplitting: false } },           // one script: lazy routes included
  },
})

const read = p => readFileSync(`${OUT}/${p.replace(/^\.?\//, '')}`, 'utf8')
const safe = js => js.replace(/<\/script/gi, '<\\/script')            // inlined code must not close its own tag
let html = read('index.html')
const scripts = [], styles = []
html = html.replace(/<script[^>]*src="([^"]+)"[^>]*><\/script>\s*/g, (tag, src) => {
  if (tag.includes('type="module"')) { scripts.push(read(src)); return '' }
  return `<script>${safe(read(src))}</script>\n`                       // theme-init: classic, before first paint
})
html = html.replace(/<link[^>]*rel="stylesheet"[^>]*href="([^"]+)"[^>]*>\s*/g, (_, href) => { styles.push(read(href)); return '' })
if (scripts.length !== 1) throw new Error(`expected one module script, found ${scripts.length}`)
const data = gzipSync(snapshot, { level: 9 }).toString('base64')
// function replacements: minified code contains $' and $` which a replacement *string* would expand
html = html
  .replace('<title>Sovereign AI Ops</title>', () => '<title>Sovereign AI Ops · demo</title>')
  .replace('</head>', () => `<style>${styles.join('\n')}</style>\n</head>`)
  .replace('</body>', () => `<script id="demo-snapshot" type="application/octet-stream">${data}</script>\n`
    + `<script type="module">${safe(scripts[0])}</script>\n</body>`)

rmSync(OUT, { recursive: true, force: true })                     // keep only the single-file result
mkdirSync(OUT)
const file = `${OUT}/sovereign-ai-ops-demo.html`
writeFileSync(file, html)
console.log(`${file}: ${(html.length / 1e6).toFixed(1)} MB (snapshot ${(snapshot.length / 1e6).toFixed(1)} MB -> ${(data.length / 1e6).toFixed(1)} MB gzip+base64)`)
