import { Fragment, useCallback, useEffect, useState } from 'react'
import type { CardRow } from '../api'
import { api } from '../api'
import { seriesVar } from '../components/Charts'
import { money0 } from '../lib/format'

function PasswordCell({ card, onSaved }: { card: CardRow; onSaved: () => void }) {
  const [editing, setEditing] = useState(false)
  const [value, setValue] = useState('')
  const [shown, setShown] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  const save = async () => {
    setBusy(true)
    setErr(null)
    try {
      await api.setCardPassword(card.id, value)
      setEditing(false)
      setValue('')
      setShown(null)
      onSaved()
    } catch (e) {
      setErr(String((e as Error).message))
    } finally {
      setBusy(false)
    }
  }

  const reveal = async () => {
    try {
      const d = await api.revealCardPassword(card.id)
      setShown(d.password)
    } catch (e) {
      setErr(String((e as Error).message))
    }
  }

  const clear = async () => {
    if (!confirm(`Forget the stored password for ${card.display_name}?`)) return
    await api.clearCardPassword(card.id).catch(() => undefined)
    setShown(null)
    onSaved()
  }

  if (editing) {
    return (
      <span style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
        <input
          className="input"
          style={{ minWidth: 160 }}
          autoFocus
          placeholder="statement password"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && value && save()}
        />
        <button className="btn primary" disabled={busy || !value} onClick={save}>Save</button>
        <button className="btn" onClick={() => { setEditing(false); setErr(null) }}>Cancel</button>
        {err && <span style={{ color: 'var(--crit)', fontSize: 12.5 }}>{err}</span>}
      </span>
    )
  }

  return (
    <span style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
      {card.password_set ? (
        <>
          <code style={{ fontVariantNumeric: 'tabular-nums' }}>{shown ?? '••••••••'}</code>
          {card.password_source === 'learned' && (
            <span className="pill" title="Discovered automatically when a statement decrypted">
              learned
            </span>
          )}
          <button className="link" onClick={() => (shown ? setShown(null) : reveal())}>
            {shown ? 'hide' : 'show'}
          </button>
          <button className="link" onClick={() => setEditing(true)}>edit</button>
          <button className="link" onClick={clear}>forget</button>
        </>
      ) : (
        <>
          <span className="sub">not stored — derived from name/DOB at parse time</span>
          <button className="link" onClick={() => setEditing(true)}>set</button>
        </>
      )}
      {err && <span style={{ color: 'var(--crit)', fontSize: 12.5 }}>{err}</span>}
    </span>
  )
}

function MailRules({ card, onSaved }: { card: CardRow; onSaved: () => void }) {
  const [senders, setSenders] = useState(card.sender_ids.join('\n'))
  const [subjects, setSubjects] = useState(card.subject_patterns.join('\n'))
  const [busy, setBusy] = useState(false)
  const [saved, setSaved] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  const lines = (value: string) => value.split(/[\n,]/).map((v) => v.trim()).filter(Boolean)
  const save = async () => {
    setBusy(true)
    setSaved(false)
    setErr(null)
    try {
      await api.setCardMailRules(card.id, lines(senders), lines(subjects))
      setSaved(true)
      onSaved()
    } catch (e) {
      setErr(String((e as Error).message))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="mail-rules">
      <div className="tile-l" style={{ marginBottom: 8 }}>Optional mailbox scan rules</div>
      <p className="hint" style={{ marginBottom: 10 }}>
        One sender address/domain and subject phrase per line. When this card is selected in the
        Pipeline, Gmail uses these rules to narrow the search before downloading messages.
      </p>
      <div className="mail-rules-grid">
        <label className="mail-rule-field">
          <span className="sub">Supported sender IDs</span>
          <textarea
            className="input mail-rule-input"
            rows={4}
            placeholder={'statements@axisbank.com\naxisbank.com'}
            value={senders}
            onChange={(e) => { setSenders(e.target.value); setSaved(false) }}
          />
        </label>
        <label className="mail-rule-field">
          <span className="sub">Possible subjects</span>
          <textarea
            className="input mail-rule-input"
            rows={4}
            placeholder={'Flipkart Axis Bank Credit Card Statement\nYour monthly card statement'}
            value={subjects}
            onChange={(e) => { setSubjects(e.target.value); setSaved(false) }}
          />
        </label>
      </div>
      <div className="mail-rule-actions">
        <button className="btn primary" disabled={busy} onClick={save}>
          {busy ? 'Saving…' : 'Save mail rules'}
        </button>
        {saved && <span className="sub">saved</span>}
        {err && <span className="mail-rule-error">{err}</span>}
      </div>
    </div>
  )
}

/** The name and date of birth every issuer password convention is built from.
 *  Stored once, encrypted, so no run needs it passed in. */
function ProfileCard({ onSaved }: { onSaved: () => void }) {
  const [name, setName] = useState('')
  const [dob, setDob] = useState('')
  const [derives, setDerives] = useState(0)
  const [saved, setSaved] = useState<string | null>(null)
  const [confirmed, setConfirmed] = useState(false)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  const load = useCallback(async () => {
    try {
      const p = await api.profile()
      setName(p.full_name)
      setDob(p.dob)
      setDerives(p.derives)
      setSaved(p.updated_at)
    } catch (e) {
      setErr(String((e as Error).message))
    }
  }, [])

  useEffect(() => { load() }, [load])

  const save = async () => {
    setBusy(true)
    setErr(null)
    setConfirmed(false)
    try {
      const r = await api.saveProfile(name.trim(), dob.trim())
      setName(r.full_name)
      setDob(r.dob)
      setDerives(r.derives)
      setSaved(r.updated_at)
      setConfirmed(true)
      onSaved()
    } catch (e) {
      setErr(String((e as Error).message))
    } finally {
      setBusy(false)
    }
  }

  const digits = dob.replace(/\D/g, '')
  const yearMissing = digits.length > 0 && digits.length < 6

  return (
    <section className="card">
      <h2>Your details</h2>
      <p className="hint">
        Issuers build statement passwords from your name and date of birth. Saved once here,
        encrypted with the same key as your other credentials, and used for every card — nothing is
        hardcoded anywhere.
      </p>

      <div className="form-row" style={{ marginTop: 0 }}>
        <input
          className="input"
          placeholder="Full name (e.g. first and last)"
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
        <input
          className="input"
          style={{ minWidth: 190 }}
          placeholder="Date of birth — DD/MM/YYYY"
          value={dob}
          onChange={(e) => setDob(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && save()}
        />
        <button type="button" className="btn primary" disabled={busy} onClick={save}>
          {busy ? 'Saving…' : 'Save'}
        </button>
        {saved && (
          <span className="sub">
            {confirmed ? 'saved and verified' : 'saved'} {saved.replace('T', ' ')}
          </span>
        )}
      </div>

      {yearMissing && (
        <div className="banner" style={{ marginTop: 12 }}>
          No year given. Several issuers require the <b>full</b> date (<code>DDMMYYYY</code>) — add
          the year so those cards can be opened too. Without it only <code>DDMM</code> forms are
          tried.
        </div>
      )}
      {err && (
        <div className="banner" style={{ borderLeftColor: 'var(--crit)', marginTop: 12 }}>{err}</div>
      )}

      <p className="hint" style={{ marginTop: 12, marginBottom: 0 }}>
        {derives > 0
          ? `Generates ${derives} password candidates per card — every name stem in lower, UPPER and
             Capitalised form, crossed with DDMM, DDMMYYYY and DDMMYY.`
          : 'Nothing saved yet, so encrypted statements can only be opened with a password entered per card below.'}
      </p>
    </section>
  )
}

export default function Cards() {
  const [cards, setCards] = useState<CardRow[]>([])
  const [open, setOpen] = useState<Record<number, boolean>>({})
  const [err, setErr] = useState<string | null>(null)

  const load = useCallback(async () => {
    try {
      setCards((await api.cards()).cards)
    } catch (e) {
      setErr(String((e as Error).message))
    }
  }, [])

  useEffect(() => { load() }, [load])

  const colourOf = (id: number) => {
    const order = [...cards].sort((a, b) => a.id - b.id).findIndex((c) => c.id === id)
    return seriesVar(order < 0 ? 0 : order)
  }

  return (
    <>
      {err && <div className="banner" style={{ borderLeftColor: 'var(--crit)' }}>{err}</div>}

      <ProfileCard onSaved={load} />

      <section className="card">
        <h2>Cards</h2>
        <p className="hint">
          One row per card, discovered from the statements themselves — cards are identified by their
          masked number, which is why re-importing a month replaces it instead of duplicating.
        </p>

        {!cards.length && <p className="sub">No cards yet — import a statement first.</p>}

        {!!cards.length && (
          <div className="tbl-wrap">
            <table>
              <thead>
                <tr>
                  <th>Card</th>
                  <th>Number</th>
                  <th style={{ textAlign: 'right' }}>Statements</th>
                  <th style={{ textAlign: 'right' }}>Txns</th>
                  <th>Latest</th>
                  <th>Statement password</th>
                </tr>
              </thead>
              <tbody>
                {cards.map((c) => (
                  <Fragment key={c.id}>
                    <tr>
                      <td>
                        <button
                          className="link"
                          style={{ marginRight: 8 }}
                          onClick={() => setOpen((o) => ({ ...o, [c.id]: !o[c.id] }))}
                          title="Show the months already parsed"
                        >
                          {open[c.id] ? '▾' : '▸'}
                        </button>
                        <i className="swatch" style={{ background: colourOf(c.id) }} /> {c.display_name}
                      </td>
                      <td><code>{c.masked_number}</code></td>
                      <td className="num">{c.statements}</td>
                      <td className="num">{c.txn_count}</td>
                      <td>{c.last_statement ?? '—'}</td>
                      <td><PasswordCell card={c} onSaved={load} /></td>
                    </tr>
                    {open[c.id] && (
                      <tr>
                        <td colSpan={6} style={{ background: 'var(--surface-2)' }}>
                          <MailRules card={c} onSaved={load} />
                          <div className="tile-l" style={{ marginBottom: 8 }}>
                            Statements already parsed and saved — newest first.
                            {' '}Months not listed have never been imported.
                          </div>
                          {!c.history.length && <p className="sub">Nothing stored for this card yet.</p>}
                          {!!c.history.length && (
                            <table>
                              <thead>
                                <tr>
                                  <th>Month</th>
                                  <th>Billing period</th>
                                  <th>File</th>
                                  <th style={{ textAlign: 'right' }}>Txns</th>
                                  <th style={{ textAlign: 'right' }}>Spend</th>
                                  <th style={{ textAlign: 'right' }}>Total due</th>
                                  <th>Imported</th>
                                </tr>
                              </thead>
                              <tbody>
                                {c.history.map((h) => (
                                  <tr key={h.id}>
                                    <td><b>{h.month}</b></td>
                                    <td>{h.period_start} → {h.period_end}</td>
                                    <td><code>{h.source_file}</code></td>
                                    <td className="num">{h.txns}</td>
                                    <td className="num">{money0(h.spend)}</td>
                                    <td className="num">{money0(h.total_dues)}</td>
                                    <td>
                                      {h.imported_at?.replace('T', ' ') ?? '—'}
                                      {h.confidence === 1 && (
                                        <> <span className="pill" style={{ color: 'var(--good-ink)' }}>100%</span></>
                                      )}
                                    </td>
                                  </tr>
                                ))}
                              </tbody>
                            </table>
                          )}
                        </td>
                      </tr>
                    )}
                  </Fragment>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="card">
        <h2>How the password is found</h2>
        <p className="hint">Tried in this order, so guessing is always the last resort.</p>
        <div className="step-row">
          <span className="step-name">1. Supplied</span>
          <span className="step-detail">a password passed with the request or on the CLI</span>
        </div>
        <div className="step-row">
          <span className="step-name">2. Stored</span>
          <span className="step-detail">
            anything saved above — set one here and that card never gets guessed again
          </span>
        </div>
        <div className="step-row">
          <span className="step-name">3. Derived</span>
          <span className="step-detail">
            documented issuer conventions from your name and date of birth
            (<code>first4+DDMM</code>, <code>FIRST4+DDMMYYYY</code>, <code>DDMMYYYY</code>, …)
          </span>
        </div>
        <div className="step-row">
          <span className="step-name">Learned</span>
          <span className="step-detail">
            when a derived password works, it is saved against that card automatically and marked{' '}
            <span className="pill">learned</span> — so next month it opens on the first attempt.
          </span>
        </div>
        <p className="hint" style={{ marginTop: 14, marginBottom: 0 }}>
          Passwords are encrypted with the same key as your mailbox credentials and are only sent to
          this page when you click <b>show</b>.
        </p>
      </section>
    </>
  )
}
