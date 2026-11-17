import { lazy, Suspense, useEffect, useState } from 'react'
import { Link, Route, Routes, useLocation, Navigate } from 'react-router-dom'
import { api, setUnauthorizedHandler, type User } from './lib/api'
import { queryClient } from './lib/queries'
import Shell from './components/Shell'
import ErrorBoundary from './components/ErrorBoundary'
import { Empty, ToastProvider } from './components/ui'
import Login from './pages/Login'

// Route-level code splitting: charts only load on pages that draw them.
const Dashboard = lazy(() => import('./pages/Dashboard'))
const Incidents = lazy(() => import('./pages/Incidents'))
const IncidentDetail = lazy(() => import('./pages/IncidentDetail'))
const Approvals = lazy(() => import('./pages/Approvals'))
const Fleet = lazy(() => import('./pages/Fleet'))
const NodeDetail = lazy(() => import('./pages/NodeDetail'))
const Metrics = lazy(() => import('./pages/Metrics'))
const Models = lazy(() => import('./pages/Models'))
const Governance = lazy(() => import('./pages/Governance'))
const AgentRun = lazy(() => import('./pages/AgentRun'))
const Security = lazy(() => import('./pages/Security'))
const Services = lazy(() => import('./pages/Services'))
const ModelDetail = lazy(() => import('./pages/ModelDetail'))
const Users = lazy(() => import('./pages/Users'))
const Account = lazy(() => import('./pages/Account'))

export default function App() {
  const location = useLocation()
  const [user, setUser] = useState<User | null | undefined>(undefined)

  useEffect(() => {
    const signOut = () => { queryClient.clear(); setUser(null) }
    setUnauthorizedHandler(signOut)
    api.me().then(setUser).catch(signOut)
  }, [])

  if (user === undefined) return null

  const logout = async () => {
    await api.logout().catch(() => undefined)
    queryClient.clear()
    setUser(null)
  }

  return (
    <ToastProvider>
      {user === null ? <Login onLogin={setUser} /> : (
        <Shell user={user} onLogout={logout}>
          <ErrorBoundary resetKey={location.pathname}>
          <Suspense fallback={<main className="content"><Empty>Loading…</Empty></main>}>
            <Routes>
              <Route path="/" element={<Dashboard />} />
              <Route path="/login" element={<Navigate to="/" replace />} />
              <Route path="/incidents" element={<Incidents />} />
              <Route path="/incidents/:id" element={<IncidentDetail user={user} />} />
              <Route path="/approvals" element={<Approvals user={user} />} />
              <Route path="/fleet" element={<Fleet />} />
              <Route path="/fleet/:node" element={<NodeDetail />} />
              <Route path="/history" element={<Metrics />} />
              <Route path="/services" element={<Services />} />
              <Route path="/models" element={<Models user={user} />} />
              <Route path="/models/:model" element={<ModelDetail />} />
              <Route path="/gateway" element={<Navigate to="/models" replace />} />
              <Route path="/security" element={<Security user={user} />} />
              <Route path="/governance" element={<Governance />} />
              <Route path="/requests" element={<Navigate to="/models?tab=requests" replace />} />
              <Route path="/evals" element={<Navigate to="/models?tab=evals" replace />} />
              <Route path="/registry" element={<Navigate to="/models?tab=registry" replace />} />
              <Route path="/agent" element={<Navigate to="/governance?tab=agent" replace />} />
              <Route path="/agent/:id" element={<AgentRun user={user} />} />
              <Route path="/audit" element={<Navigate to="/governance?tab=audit" replace />} />
              <Route path="/policies" element={<Navigate to="/governance?tab=policies" replace />} />
              <Route path="/users" element={<Users user={user} />} />
              <Route path="/account" element={<Account user={user} />} />
              <Route path="*" element={<main className="content"><h1>Page not found</h1><Link to="/">Back to the dashboard</Link></main>} />
            </Routes>
          </Suspense>
          </ErrorBoundary>
        </Shell>
      )}
    </ToastProvider>
  )
}
