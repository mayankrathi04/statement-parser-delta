import { useCallback, useEffect, useState } from 'react'
import { api, type Mailbox, type Member } from '../api'
import IconButton from '../components/IconButton'

const DOT: Record<string, string> = {
  connected: 'var(--success-fill)',
  failed: 'var(--critical)',
  unknown: 'var(--text-muted)',
}

export default function Connections({ onChanged, members }: { onChanged: () => void; members: Member[] }) {
  const [boxes, setBoxes] = useState<Mailbox[]>([])
  const [keyFile, setKeyFile] = useState('')
  const [envConfigured, setEnv] = useState(false)
  const [address, setAddress] = useState('')
  const [secret, setSecret] = useState('')
  const [memberId, setMemberId] = useState<number | undefined>(members[0]?.id)
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null)

  const load = useCallback(async () => {
    try {
      const r = await api.mailboxes()
      setBoxes(r.mailboxes)
      setKeyFile(r.key_file)
      setEnv(r.env_configured)
    } catch (e) {
      setMsg({ ok: false, text: String((e as Error).message) })
    }
  }, [])

  useEffect(() => { load() }, [load])

  const add = async () => {
    setBusy(true)
    setMsg(null)
    try {
      const d = await api.addMailbox(address.trim(), secret, memberId)
      setMsg({ ok: true, text: `${address} — ${d.detail}` })
      setAddress('')
      setSecret('')
      onChanged()
    } catch (e) {
      setMsg({ ok: false, text: String((e as Error).message) })
    } finally {
      setBusy(false)
      load()
    }
  }

  const test = async (id: number) => {
    setBusy(true)
    try {
      const d = await api.testMailbox(id)
      setMsg({ ok: d.status === 'connected', text: d.detail })
    } catch (e) {
      setMsg({ ok: false, text: String((e as Error).message) })
    } finally {
      setBusy(false)
      load()
    }
  }

  /** Which pipelines sweep this mailbox. Written straight through — the row is
   *  reloaded from the server, so a rejected change cannot linger on screen. */
  const setScope = async (box: Mailbox, cards: boolean, bank: boolean) => {
    setMsg(null)
    try {
      await api.setMailboxScope(box.id, cards, bank)
      if (!cards && !bank) {
        setMsg({
          ok: true,
          text: `${box.address} is connected but no longer scanned by either pipeline.`,
        })
      }
    } catch (e) {
      setMsg({ ok: false, text: String((e as Error).message) })
    } finally {
      load()
    }
  }

  const remove = async (id: number, addr: string) => {
    if (!confirm(`Disconnect ${addr}? Statements already imported are kept.`)) return
    await api.removeMailbox(id).catch(() => undefined)
    load()
    onChanged()
  }

  return (
    <>
      <section className="card">
        <h2>Connected mailboxes</h2>
        <p className="hint">
          Statements are fetched read-only over IMAP. Nothing in the mailbox is read beyond the
          statement mails, and nothing is ever modified or deleted.
        </p>
        <p className="hint">
          Tick what each mailbox actually receives. A card scan searches only the mailboxes marked
          for card statements and a bank scan only those marked for bank statements, so an account
          that never gets one of the two is not searched for it — which is most of what makes a
          scan slow. Untick both to pause a mailbox without disconnecting it.
        </p>

        {!boxes.length && <p className="sub">No mailboxes connected yet.</p>}

        {boxes.map((m) => (
          <div className="conn-row" key={m.id}>
            <span className="dot" style={{ background: DOT[m.status], marginTop: 0 }} />
            <span>
              <div className="conn-addr">{m.address}</div>
              <div className="step-detail">Member: {m.member_name ?? 'Unassigned'}</div>
              <div className="step-detail">
                {m.status === 'connected' && m.last_sync
                  ? `last sync ${m.last_sync.replace('T', ' ')}`
                  : m.last_error
                    ? m.last_error
                    : m.last_checked
                      ? `checked ${m.last_checked.replace('T', ' ')}`
                      : 'never checked'}
              </div>
            </span>
            <span className="spacer" />
            <span className="conn-scope">
              <label title="Search this mailbox on credit-card scans">
                <input
                  type="checkbox"
                  checked={m.use_for_cards}
                  onChange={(event) => setScope(m, event.target.checked, m.use_for_bank)}
                />{' '}
                Card statements
              </label>
              <label title="Search this mailbox on bank account scans">
                <input
                  type="checkbox"
                  checked={m.use_for_bank}
                  onChange={(event) => setScope(m, m.use_for_cards, event.target.checked)}
                />{' '}
                Bank statements
              </label>
            </span>
            <select className="input" value={m.member_id ?? ''} onChange={async (event) => {
              await api.assignMailboxMember(m.id, Number(event.target.value)); void load()
            }}>{members.map((member) => <option key={member.id} value={member.id}>{member.name}</option>)}</select>
            {!m.secret_ok && (
              <span className="badge" style={{ color: 'var(--critical)' }}>
                secret unreadable — re-add
              </span>
            )}
            <span className="badge" style={{ color: DOT[m.status] }}>{m.status}</span>
            <span className="row-actions">
              <IconButton label="Test" icon="test" disabled={busy}
                title="Test this connection" onClick={() => test(m.id)} />
              <IconButton label="Disconnect" icon="disconnect" danger disabled={busy}
                title="Disconnect this mailbox" onClick={() => remove(m.id, m.address)} />
            </span>
          </div>
        ))}

        <div className="form-row">
          <select className="input" value={memberId ?? ''} onChange={(event) => setMemberId(Number(event.target.value))}>
            {members.map((member) => <option key={member.id} value={member.id}>{member.name}</option>)}
          </select>
          <input
            className="input"
            placeholder="you@gmail.com"
            value={address}
            autoComplete="off"
            onChange={(e) => setAddress(e.target.value)}
          />
          <input
            className="input"
            type="password"
            placeholder="16-character app password"
            value={secret}
            autoComplete="new-password"
            onChange={(e) => setSecret(e.target.value)}
          />
          <button
            className="btn primary"
            disabled={busy || !address.trim() || !secret.trim()}
            onClick={add}
          >
            {busy ? 'Checking…' : 'Connect'}
          </button>
          {busy && <span className="sub">contacting Gmail — this can take a few seconds</span>}
        </div>

        {msg && (
          <div
            className="banner"
            style={{ borderLeftColor: msg.ok ? 'var(--success-fill)' : 'var(--critical)', marginTop: 14 }}
          >
            {msg.text}
          </div>
        )}
      </section>

      <section className="card">
        <h2>How to get an app password</h2>
        <p className="hint">Gmail rejects your normal password over IMAP; it needs a per-app one.</p>
        <ol style={{ margin: 0, paddingLeft: 20, color: 'var(--text-secondary)', fontSize: 13.5, lineHeight: 1.9 }}>
          <li>Turn on 2-Step Verification for the account.</li>
          <li>
            Visit{' '}
            <a href="https://myaccount.google.com/apppasswords" target="_blank" rel="noreferrer">
              myaccount.google.com/apppasswords
            </a>{' '}
            and create one named “sparser”.
          </li>
          <li>Paste the 16 characters above — spaces are fine, they get stripped.</li>
          <li>Repeat for your second Gmail account.</li>
        </ol>
      </section>

      <section className="card">
        <h2>Where credentials live</h2>
        <p className="hint">
          App passwords are encrypted before they touch the database and are never sent back to this
          page — only their status is.
        </p>
        <div className="step-row">
          <span className="step-name">Key file</span>
          <span className="step-detail"><code>{keyFile}</code> (0600, outside the project)</span>
        </div>
        <div className="step-row">
          <span className="step-name">Database</span>
          <span className="step-detail">holds the encrypted secret only; useless without the key file</span>
        </div>
        {envConfigured && (
          <div className="step-row">
            <span className="step-name">Environment</span>
            <span className="step-detail">
              <code>SPARSER_GMAIL</code> is also set — those accounts are used in addition to the
              ones listed above, which is the route for a scheduled job on a server.
            </span>
          </div>
        )}
      </section>
    </>
  )
}
