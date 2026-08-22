import { Fragment, useCallback, useEffect, useState } from 'react'
import { api, type BankAccount, type Member } from '../api'
import { seriesVar } from '../components/Charts'
import IconButton, { IconLink } from '../components/IconButton'
import ScanDefaults from '../components/ScanDefaults'
import { money2 } from '../lib/format'

/** The password that opens this account's statement PDFs — and, for HDFC, the
 *  smart statement link's gate, which asks for the same thing. */
function PasswordCell({ account, onSaved }: { account: BankAccount; onSaved: () => void }) {
  const [editing, setEditing] = useState(false)
  const [value, setValue] = useState('')
  const [shown, setShown] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const save = async () => {
    setBusy(true)
    setError(null)
    try {
      await api.setBankAccountPassword(account.id, value)
      setEditing(false)
      setValue('')
      setShown(null)
      onSaved()
    } catch (caught) {
      setError(String((caught as Error).message))
    } finally {
      setBusy(false)
    }
  }

  const reveal = async () => {
    try {
      setShown((await api.revealBankAccountPassword(account.id)).password)
    } catch (caught) {
      setError(String((caught as Error).message))
    }
  }

  const clear = async () => {
    if (!confirm(`Forget the stored password for ${account.display_name}?`)) return
    await api.clearBankAccountPassword(account.id).catch(() => undefined)
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
          onChange={(event) => setValue(event.target.value)}
          onKeyDown={(event) => event.key === 'Enter' && value && save()}
        />
        <span className="row-actions">
          <IconButton label="Save" icon="save" disabled={busy || !value} onClick={save} />
          <IconButton label="Cancel" icon="cancel"
            onClick={() => { setEditing(false); setError(null) }} />
        </span>
        {error && <span style={{ color: 'var(--critical)', fontSize: 12.5 }}>{error}</span>}
      </span>
    )
  }

  return (
    <span style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
      {account.password_set ? (
        <>
          <code style={{ fontVariantNumeric: 'tabular-nums' }}>{shown ?? '••••••••'}</code>
          {account.password_source === 'learned' && (
            <span className="pill" title="Discovered automatically when a statement decrypted">
              learned
            </span>
          )}
          <span className="row-actions">
            <IconButton
              label={shown ? 'Hide' : 'Show'} icon={shown ? 'hide' : 'show'}
              title={shown ? 'Hide the stored password' : 'Show the stored password'}
              onClick={() => (shown ? setShown(null) : reveal())}
            />
            <IconButton label="Edit" icon="edit" title="Edit the stored password"
              onClick={() => setEditing(true)} />
            <IconButton label="Forget" icon="forget" danger title="Forget the stored password"
              onClick={clear} />
          </span>
        </>
      ) : (
        <>
          <span className="sub">not stored — derived from name/DOB at parse time</span>
          <IconButton label="Set" icon="set" title="Store a statement password"
            onClick={() => setEditing(true)} />
        </>
      )}
      {error && <span style={{ color: 'var(--critical)', fontSize: 12.5 }}>{error}</span>}
    </span>
  )
}

/** Narrows the mailbox search for one account, exactly as a card can. */
function MailRules({ account, onSaved }: { account: BankAccount; onSaved: () => void }) {
  const [senders, setSenders] = useState(account.sender_ids.join('\n'))
  const [subjects, setSubjects] = useState(account.subject_patterns.join('\n'))
  const [busy, setBusy] = useState(false)
  const [saved, setSaved] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const lines = (value: string) => value.split(/[\n,]/).map((v) => v.trim()).filter(Boolean)
  const save = async () => {
    setBusy(true)
    setSaved(false)
    setError(null)
    try {
      await api.setBankAccountMailRules(account.id, lines(senders), lines(subjects))
      setSaved(true)
      onSaved()
    } catch (caught) {
      setError(String((caught as Error).message))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="mail-rules">
      <div className="tile-l" style={{ marginBottom: 8 }}>Optional mailbox scan rules</div>
      <p className="hint" style={{ marginBottom: 10 }}>
        One sender address/domain and subject phrase per line. When this account is selected in
        Bank Pipeline, Gmail uses these rules to narrow the search before downloading messages.
        Both boxes must be filled for the rules to replace the bank defaults.
      </p>
      <div className="mail-rules-grid">
        <label className="mail-rule-field">
          <span className="sub">Supported sender IDs</span>
          <textarea
            className="input mail-rule-input"
            rows={4}
            placeholder={'estatement@hdfcbank.net\nstatements@yourbank.com'}
            value={senders}
            onChange={(event) => { setSenders(event.target.value); setSaved(false) }}
          />
        </label>
        <label className="mail-rule-field">
          <span className="sub">Possible subjects</span>
          <textarea
            className="input mail-rule-input"
            rows={4}
            placeholder={'HDFC Bank Account Statement\nYour Smart Statement'}
            value={subjects}
            onChange={(event) => { setSubjects(event.target.value); setSaved(false) }}
          />
        </label>
      </div>
      <div className="mail-rule-actions">
        <button className="btn primary" disabled={busy} onClick={save}>
          {busy ? 'Saving…' : 'Save mail rules'}
        </button>
        {saved && <span className="sub">saved</span>}
        {error && <span className="mail-rule-error">{error}</span>}
      </div>
    </div>
  )
}

export default function BankAccounts({
  roster, members,
}: {
  /** Every member, so an account can be reassigned to one not currently selected. */
  roster: Member[]
  members: Set<number>
}) {
  const [accounts, setAccounts] = useState<BankAccount[]>([])
  const [open, setOpen] = useState<Record<number, boolean>>({})
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    try {
      setAccounts((await api.bankBootstrap(members)).accounts)
      setError(null)
    } catch (caught) {
      setError(String((caught as Error).message))
    }
  }, [members])

  useEffect(() => { load() }, [load])
  const colourOf = (id: number) => seriesVar(
    [...accounts].sort((a, b) => a.id - b.id).findIndex((account) => account.id === id),
  )

  return (
    <>
      {error && <div className="banner" style={{ borderLeftColor: 'var(--critical)' }}>{error}</div>}
      <section className="card">
        <h2>Bank accounts</h2>
        <p className="hint">
          Accounts are discovered from imported statements. Outside the retained source PDF, only a
          one-way fingerprint and the last four digits are stored for identity and display.
        </p>
        <p className="hint">
          Expand an account to narrow which mails a bank scan searches for it. The statement
          password is stored encrypted and tried before anything is derived — for an HDFC smart
          statement it is also what opens the link's password gate.
        </p>
        {!accounts.length && (
          <p className="empty">
            {members.size && members.size < roster.length
              ? 'No bank accounts for the selected member. Widen the selector at the top to see the rest.'
              : 'No bank accounts yet. Upload an account statement in Bank Pipeline.'}
          </p>
        )}
        {!!accounts.length && (
          <div className="tbl-wrap">
            <table>
              <thead>
                <tr>
                  <th>Account</th><th>Member</th><th>Number</th><th>Type</th><th>Branch</th>
                  <th style={{ textAlign: 'right' }}>Statements</th>
                  <th style={{ textAlign: 'right' }}>Txns</th><th>Coverage</th>
                  <th>Statement password</th>
                </tr>
              </thead>
              <tbody>
                {accounts.map((account) => (
                  <Fragment key={account.id}>
                    <tr>
                      <td>
                        <IconButton
                          label={open[account.id] ? 'Collapse' : 'Expand'}
                          icon={open[account.id] ? 'collapse' : 'expand'}
                          title={open[account.id]
                            ? 'Hide imported statements'
                            : 'Show imported statements'}
                          onClick={() => setOpen((current) => ({
                            ...current, [account.id]: !current[account.id],
                          }))}
                        />{' '}
                        <i className="swatch" style={{ background: colourOf(account.id) }} />{' '}
                        {account.display_name}
                      </td>
                      <td><select value={account.member_id ?? ''} onChange={async (event) => {
                        await api.assignBankAccountMember(account.id, Number(event.target.value)); void load()
                      }}>{roster.map((member) => <option key={member.id} value={member.id}>{member.name}</option>)}</select></td>
                      <td><code>{account.masked_number}</code></td>
                      <td>{account.account_type ?? account.product ?? '—'}</td>
                      <td>{account.branch ?? '—'}</td>
                      <td className="num">{account.statements}</td>
                      <td className="num">{account.txn_count}</td>
                      <td>
                        {account.first_txn && account.last_txn
                          ? `${account.first_txn} → ${account.last_txn}` : '—'}
                      </td>
                      <td><PasswordCell account={account} onSaved={load} /></td>
                    </tr>
                    {open[account.id] && (
                      <tr>
                        <td colSpan={9} style={{ background: 'var(--surface-2)' }}>
                          <MailRules account={account} onSaved={load} />
                          <div className="tile-l" style={{ marginBottom: 8 }}>
                            Statements present for this account — newest first
                          </div>
                          <table>
                            <thead>
                              <tr>
                                <th>Statement period</th><th>Rows present</th><th>File</th>
                                <th style={{ textAlign: 'right' }}>Txns</th>
                                <th style={{ textAlign: 'right' }}>Withdrawals</th>
                                <th style={{ textAlign: 'right' }}>Deposits</th>
                                <th style={{ textAlign: 'right' }}>Closing</th><th>Imported</th>
                              </tr>
                            </thead>
                            <tbody>
                              {account.history.map((statement) => (
                                <tr key={statement.id}>
                                  <td><b>{statement.period_start} → {statement.period_end}</b></td>
                                  <td>
                                    {statement.coverage_start && statement.coverage_end
                                      ? `${statement.coverage_start} → ${statement.coverage_end}` : '—'}
                                  </td>
                                  <td>
                                    <code>{statement.source_file}</code>{' '}
                                    <IconLink
                                      label="View PDF" icon="open"
                                      title="View PDF — opens this statement in a new tab"
                                      href={api.bankStatementPdfUrl(statement.id)}
                                      target="_blank" rel="noreferrer"
                                    />
                                  </td>
                                  <td className="num">{statement.txns}</td>
                                  <td className="num">{money2(statement.withdrawals)}</td>
                                  <td className="num cre">{money2(statement.deposits)}</td>
                                  <td className="num">{money2(statement.closing_balance)}</td>
                                  <td>
                                    {statement.imported_at.replace('T', ' ')}{' '}
                                    {statement.confidence === 1 && (
                                      <span className="pill" style={{ color: 'var(--success)' }}>100%</span>
                                    )}
                                  </td>
                                </tr>
                              ))}
                            </tbody>
                          </table>
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

      <ScanDefaults kind="bank" />
    </>
  )
}
