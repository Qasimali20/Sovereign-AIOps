import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Built into dist/ and served by the FastAPI app on :5170.
// `npm run dev` proxies /api to the running backend.
export default defineConfig({
  plugins: [react()],
  // fixed at build time so the offline-demo branch (src/demo.ts) is dropped from the production bundle
  define: { 'import.meta.env.VITE_DEMO': JSON.stringify(process.env.VITE_DEMO ?? '') },
  server: { proxy: { '/api': 'http://127.0.0.1:5170' } },
  build: { outDir: 'dist', emptyOutDir: true },
})
