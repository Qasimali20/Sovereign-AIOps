import { useState, type FormEvent } from 'react'
import { AlertCircle, Lock, ScanSearch, ShieldCheck, Server } from 'lucide-react'
import { api, type User } from '../lib/api'

export default function Login({ onLogin }: { onLogin: (u: User) => void }) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    setBusy(true)
    setError('')
    try {
      onLogin(await api.login(username, password))
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="login-page">
      <div className="login-art">
        <div className="brand">Sovereign <span className="brand-accent">AI Ops</span></div>
        <div>
          <h1>Private, policy-governed operations for the <em>DGX Spark</em> fleet.</h1>
          <p>Agents investigate incidents with self-hosted models, propose fixes, and act only after a
            deterministic policy check and a human approval. Every step is recorded.</p>
        </div>
        <div className="login-feats">
          <div><span className="ic"><Server size={16} /></span><span>Inference, telemetry and audit data stay on the four on-prem nodes.</span></div>
          <div><span className="ic"><ScanSearch size={16} /></span><span>Evidence-backed diagnosis that separates facts from hypotheses.</span></div>
          <div><span className="ic"><ShieldCheck size={16} /></span><span>The model proposes. Policy decides. People approve.</span></div>
        </div>
        <span className="block" />
      </div>
      <div className="login-form-wrap">
        <form className="login-form" onSubmit={submit}>
          <div className="stack" style={{ gap: 6 }}>
            <h2>Sign in</h2>
            <span className="muted">Use your operations console account.</span>
          </div>
          <label className="field">Username
            <input autoFocus autoComplete="username" value={username} onChange={e => setUsername(e.target.value)} />
          </label>
          <label className="field">Password
            <input type="password" autoComplete="current-password" value={password} onChange={e => setPassword(e.target.value)} />
          </label>
          {error && <div className="err"><AlertCircle size={15} />{error}</div>}
          <button className="btn btn-primary" style={{ height: 40 }} disabled={busy || !username || !password}>
            <Lock size={15} />{busy ? 'Signing in…' : 'Sign in'}
          </button>
          <span className="muted small">Access is limited to the office network and Tailscale.</span>
        </form>
      </div>
    </div>
  )
}
