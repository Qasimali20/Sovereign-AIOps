import { useState, type FormEvent } from 'react'
import { KeyRound, Trash2, UserPlus, Users as UsersIcon } from 'lucide-react'
import type { Role, User } from '../lib/api'
import { useDeleteUser, useSaveUser, useUsers } from '../lib/queries'
import { Badge, Card, Empty, Modal, PageHead, useToast } from '../components/ui'
import { dateTime } from '../lib/format'

const ROLE_INFO: Record<Role, string> = {
  viewer: 'Read incidents, evidence, fleet and audit.',
  approver: 'Viewer, plus approve/reject actions and close incidents.',
  admin: 'Approver, plus manage users.',
}

type Editing = { username: string; role: Role; isNew: boolean }

export default function Users({ user }: { user: User }) {
  const toast = useToast()
  const isAdmin = user.role === 'admin'
  const { data } = useUsers(isAdmin)
  const save = useSaveUser()
  const del = useDeleteUser()
  const [editing, setEditing] = useState<Editing | null>(null)
  const [password, setPassword] = useState('')
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null)

  if (!isAdmin) return <main className="content"><PageHead title="Users" /><div className="callout info">Only admins can manage users.</div></main>

  const submit = (e: FormEvent) => {
    e.preventDefault()
    if (!editing) return
    save.mutate({ username: editing.username.trim(), password, role: editing.role }, {
      onSuccess: () => { toast('good', `${editing.isNew ? 'Created' : 'Updated'} ${editing.username}`); setEditing(null); setPassword('') },
      onError: err => toast('crit', err.message),
    })
  }

  return (
    <main className="content">
      <PageHead title="Users"
        actions={<button className="btn btn-primary" onClick={() => { setEditing({ username: '', role: 'viewer', isNew: true }); setPassword('') }}><UserPlus size={15} />Add user</button>} />
      <div className="grid g-3">
        {(Object.keys(ROLE_INFO) as Role[]).map(r => (
          <div key={r} className="card kpi"><div className="label"><Badge tone={r === 'admin' ? 'info' : r === 'approver' ? 'warn' : 'neutral'}>{r}</Badge></div><div className="small ink2">{ROLE_INFO[r]}</div></div>
        ))}
      </div>
      <Card bodyClass="card-body tight">
        {data === undefined ? <Empty>Loading…</Empty> : data.length === 0 ? <Empty icon={<UsersIcon size={26} />}>No users.</Empty> : (
          <table className="table">
            <thead><tr><th>User</th><th>Role</th><th className="hide-sm">Created</th><th /></tr></thead>
            <tbody>
              {data.map(u => (
                <tr key={u.username}>
                  <td><div className="row"><span className="avatar">{u.username.slice(0, 2)}</span><span className="primary">{u.username}</span>{u.username === user.username && <span className="muted small">(you)</span>}</div></td>
                  <td><Badge tone={u.role === 'admin' ? 'info' : u.role === 'approver' ? 'warn' : 'neutral'}>{u.role}</Badge></td>
                  <td className="muted hide-sm">{dateTime(u.created)}</td>
                  <td className="right-align nowrap">
                    <button className="btn btn-sm btn-ghost" onClick={() => { setEditing({ username: u.username, role: u.role, isNew: false }); setPassword('') }}><KeyRound size={14} />Edit</button>
                    {u.username !== user.username && <button className="btn btn-sm btn-ghost" onClick={() => setConfirmDelete(u.username)}><Trash2 size={14} />Remove</button>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>

      {editing && (
        <Modal title={editing.isNew ? 'Add user' : `Edit ${editing.username}`} onClose={() => setEditing(null)}
          footer={<>
            <button className="btn btn-ghost" onClick={() => setEditing(null)}>Cancel</button>
            <button className="btn btn-primary" form="user-form" disabled={save.isPending || password.length < 8 || !editing.username.trim()}>Save</button>
          </>}>
          <form id="user-form" className="form-grid" onSubmit={submit}>
            {editing.isNew && (
              <label className="field">Username
                <input autoFocus value={editing.username} pattern="[A-Za-z0-9._\-]+" onChange={e => setEditing({ ...editing, username: e.target.value })} />
                <span className="help">Letters, digits, dot, dash and underscore.</span>
              </label>
            )}
            <label className="field">Role
              <select value={editing.role} onChange={e => setEditing({ ...editing, role: e.target.value as Role })}>
                {(Object.keys(ROLE_INFO) as Role[]).map(r => <option key={r} value={r}>{r}: {ROLE_INFO[r]}</option>)}
              </select>
            </label>
            <label className="field">{editing.isNew ? 'Password' : 'New password'}
              <input type="password" autoComplete="new-password" value={password} onChange={e => setPassword(e.target.value)} />
              <span className="help">At least 8 characters.{!editing.isNew && ' Saving sets this password.'}</span>
            </label>
          </form>
        </Modal>
      )}

      {confirmDelete && (
        <Modal title={`Remove ${confirmDelete}?`} onClose={() => setConfirmDelete(null)}
          footer={<>
            <button className="btn btn-ghost" onClick={() => setConfirmDelete(null)}>Cancel</button>
            <button className="btn btn-danger" disabled={del.isPending} onClick={() => del.mutate(confirmDelete, {
              onSuccess: () => { toast('good', `Removed ${confirmDelete}`); setConfirmDelete(null) },
              onError: err => toast('crit', err.message),
            })}>Remove user</button>
          </>}>
          <p className="ink2">They will be signed out and can no longer sign in. Their past approvals stay in the audit log.</p>
        </Modal>
      )}
    </main>
  )
}
