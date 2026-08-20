import { useCallback, useEffect, useState } from 'react'
import { api, type MemberDetail } from '../api'
import IconButton from '../components/IconButton'

function AddMember({ onAdded }: { onAdded: () => void }) {
  const [name, setName] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const add = async () => {
    setBusy(true)
    setError(null)
    try {
      await api.addMember(name.trim())
      setName('')
      onAdded()
    } catch (caught) {
      setError(String((caught as Error).message))
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="card">
      <h2>Add a member</h2>
      <p className="hint">
        A member is one person in the household. Cards, bank accounts and mailboxes each belong
        to one, which is what keeps their spending separable in the analysis tabs.
      </p>
      <div className="form-row" style={{ marginTop: 0 }}>
        <input
          className="input"
          style={{ minWidth: 240 }}
          placeholder="Name — e.g. a partner, a parent"
          value={name}
          onChange={(event) => setName(event.target.value)}
          onKeyDown={(event) => event.key === 'Enter' && name.trim() && add()}
        />
        <button className="btn primary" disabled={busy || !name.trim()} onClick={add}>
          {busy ? 'Adding…' : 'Add member'}
        </button>
      </div>
      {error && (
        <div className="banner" style={{ borderLeftColor: 'var(--critical)', marginTop: 12 }}>{error}</div>
      )}
    </section>
  )
}

function MemberRow({
  member, total, onChanged,
}: {
  member: MemberDetail
  total: number
  onChanged: () => void
}) {
  const [editing, setEditing] = useState(false)
  const [name, setName] = useState(member.name)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const attached = member.cards + member.bank_accounts + member.mailboxes

  const run = async (action: () => Promise<unknown>) => {
    setBusy(true)
    setError(null)
    try {
      await action()
      setEditing(false)
      onChanged()
    } catch (caught) {
      setError(String((caught as Error).message))
    } finally {
      setBusy(false)
    }
  }

  const remove = () => {
    if (!confirm(`Remove ${member.name}? Nothing is deleted — this only works when nothing is filed under them.`)) return
    void run(() => api.removeMember(member.id))
  }

  return (
    <>
      <tr>
        <td>
          {editing ? (
            <input
              className="input"
              autoFocus
              value={name}
              onChange={(event) => setName(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === 'Enter' && name.trim()) void run(() => api.renameMember(member.id, name.trim()))
                if (event.key === 'Escape') { setEditing(false); setName(member.name) }
              }}
            />
          ) : (
            <b>{member.name}</b>
          )}
          {!!member.is_default && <> <span className="pill">default</span></>}
        </td>
        <td className="num">{member.cards}</td>
        <td className="num">{member.bank_accounts}</td>
        <td className="num">{member.mailboxes}</td>
        <td>{member.created_at?.slice(0, 10) ?? '—'}</td>
        <td>
          <span className="row-actions">
            {editing ? (
              <>
                <IconButton
                  label="Save" icon="save" disabled={busy || !name.trim()}
                  onClick={() => void run(() => api.renameMember(member.id, name.trim()))}
                />
                <IconButton label="Cancel" icon="cancel"
                  onClick={() => { setEditing(false); setName(member.name) }} />
              </>
            ) : (
              <IconButton label="Rename" icon="edit" onClick={() => setEditing(true)} />
            )}
            {!member.is_default && (
              <IconButton
                label="Make default" icon="default" disabled={busy}
                title="Make default — imports fall back to this member when the selector names no single one"
                onClick={() => void run(() => api.setDefaultMember(member.id))}
              />
            )}
            {!member.is_default && total > 1 && attached === 0 && (
              <IconButton label="Remove" icon="delete" danger disabled={busy} onClick={remove} />
            )}
          </span>
        </td>
      </tr>
      {error && (
        <tr>
          <td colSpan={6}>
            <div className="banner" style={{ borderLeftColor: 'var(--critical)' }}>{error}</div>
          </td>
        </tr>
      )}
    </>
  )
}

export default function Members({ onChanged }: { onChanged: () => void }) {
  const [members, setMembers] = useState<MemberDetail[]>([])
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    try {
      setMembers((await api.members()).members)
      setError(null)
      onChanged()
    } catch (caught) {
      setError(String((caught as Error).message))
    }
  }, [onChanged])

  useEffect(() => { void load() }, [load])

  return (
    <>
      {error && <div className="banner" style={{ borderLeftColor: 'var(--critical)' }}>{error}</div>}

      <AddMember onAdded={load} />

      <section className="card">
        <h2>Members</h2>
        <p className="hint">
          The selector at the top of every page filters the analysis by these members. When it
          names exactly one, uploads and imports are filed under them; otherwise they fall back
          to the default marked below.
        </p>
        <div className="tbl-wrap">
          <table>
            <thead>
              <tr>
                <th>Member</th>
                <th style={{ textAlign: 'right' }}>Cards</th>
                <th style={{ textAlign: 'right' }}>Bank accounts</th>
                <th style={{ textAlign: 'right' }}>Mailboxes</th>
                <th>Added</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {members.map((member) => (
                <MemberRow
                  key={member.id}
                  member={member}
                  total={members.length}
                  onChanged={load}
                />
              ))}
            </tbody>
          </table>
        </div>
        <p className="hint" style={{ marginTop: 14, marginBottom: 0 }}>
          A member can only be removed once nothing is filed under them — reassign their cards,
          accounts and mailboxes first, on the Cards, Bank Accounts and Connections tabs. Removing
          a member never deletes statements.
        </p>
      </section>
    </>
  )
}
