import { useState, type FormEvent } from 'react'

import type { User } from '../lib/api'
import { useChangePassword } from '../lib/queries'
import { Badge, Card, PageHead, useToast } from '../components/ui'

export default function Account({ user }: { user: User }) {
  const toast = useToast()
  const change = useChangePassword()
  const [current, setCurrent] = useState('')
  const [next, setNext] = useState('')
  const [repeat, setRepeat] = useState('')
  const mismatch = repeat.length > 0 && next !== repeat

  const submit = (e: FormEvent) => {
    e.preventDefault()
    change.mutate({ current, next }, {
      onSuccess: () => { toast('good', 'Password changed'); setCurrent(''); setNext(''); setRepeat('') },
      onError: err => toast('crit', err.message),
    })
  }

  return (
    <main className="content">
      <PageHead title="Account" />
      <div className="grid g-2">
        <Card title="Profile">
          <dl className="kv">
            <dt>Username</dt><dd>{user.username}</dd>
            <dt>Role</dt><dd><Badge tone={user.role === 'admin' ? 'info' : user.role === 'approver' ? 'warn' : 'neutral'}>{user.role}</Badge></dd>
            <dt>Session</dt><dd>12 hours, HttpOnly cookie</dd>
          </dl>
        </Card>
        <Card title="Change password">
          <form className="form-grid" onSubmit={submit}>
            <label className="field">Current password<input type="password" autoComplete="current-password" value={current} onChange={e => setCurrent(e.target.value)} /></label>
            <label className="field">New password<input type="password" autoComplete="new-password" value={next} onChange={e => setNext(e.target.value)} /><span className="help">At least 8 characters.</span></label>
            <label className="field">Repeat new password<input type="password" autoComplete="new-password" value={repeat} onChange={e => setRepeat(e.target.value)} />
              {mismatch && <span className="err">Passwords do not match.</span>}</label>
            <div><button className="btn btn-primary" disabled={change.isPending || !current || next.length < 8 || next !== repeat}>Update password</button></div>
          </form>
        </Card>
      </div>
    </main>
  )
}
