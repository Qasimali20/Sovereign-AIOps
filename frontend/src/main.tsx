import '@fontsource-variable/geist'
import '@fontsource-variable/jetbrains-mono'
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter, HashRouter } from 'react-router-dom'
import { QueryClientProvider } from '@tanstack/react-query'
import { queryClient } from './lib/queries'
import App from './App'
import { DEMO } from './lib/api'
import './index.css'

// The offline demo opens from a file: hash routes, and the snapshot (and its clock) loaded before the first render.
const Router = DEMO ? HashRouter : BrowserRouter
if (import.meta.env.VITE_DEMO === '1') await (await import('./demo')).init()

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <Router>
        <App />
      </Router>
    </QueryClientProvider>
  </StrictMode>,
)
