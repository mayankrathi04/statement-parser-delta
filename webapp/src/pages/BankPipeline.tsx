import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  api, type BankAccount, type IngestFile, type Mailbox, type Member, type Pending, type Run,
  type Step,
} from '../api'
import AccountSelect from '../components/AccountSelect'
import { seriesVar } from '../components/Charts'
import { IconLink } from '../components/IconButton'
import { importTarget } from '../lib/members'

/** Where the statements come from. Mail is searched by statement period; an
 *  upload and a path are taken as given, so the period row is hidden for them. */
type ScanSource = 'mail' | 'upload' | 'disk'

/** Which window a mail scan covers; picks the date inputs to show. */
type ScanMode = 'this' | 'month' | 'range' | 'last12'

const SCAN_SOURCES: { value: ScanSource; label: string }[] = [
  { value: 'mail', label: 'Scan from connection' },
  { value: 'upload', label: 'Upload statements' },
  { value: 'disk', label: 'Scan from file' },
]

const SCAN_MODES: { value: ScanMode; label: string }[] = [
  { value: 'this', label: 'Scan this month' },
  { value: 'month', label: 'Scan a specific month' },
  { value: 'range', label: 'Scan a month range' },
  { value: 'last12', label: 'Scan the last 12 months' },
]

const COLOUR: Record<string, string> = {
  ok: 'var(--success-fill)', done: 'var(--success-fill)', failed: 'var(--critical)',
  skipped: 'var(--baseline)', pending: 'var(--warning)', warning: 'var(--warning)', running: 'var(--warning)',
}

function StepRow({ step }: { step: Step }) {
  return (
    <div className="step-row">
      <span className="dot" style={{ background: COLOUR[step.status] ?? 'var(--text-muted)' }} />
      <span className="step-name">{step.name}</span>
      <span className="step-detail">
        {step.detail}{step.ms != null && step.ms > 0 ? ` · ${step.ms} ms` : ''}
      </span>
    </div>
  )
}

function Result({ file }: { file: IngestFile }) {
  const [open, setOpen] = useState(file.status !== 'ok')
  return (
    <div className="card" style={{ marginBottom: 10 }}>
      <div className="file-head" onClick={() => setOpen((value) => !value)}>
        <span className="dot" style={{ background: COLOUR[file.status] ?? 'var(--text-muted)' }} />
        <span className="file-name">{file.filename}</span>
        {file.pdf_available && (
          // Inside a header that toggles the card, so the click must not also
          // collapse the row it was aimed at.
          <span onClick={(event) => event.stopPropagation()}>
            <IconLink
              label="View PDF" icon="open"
              title="View PDF — opens this statement in a new tab"
              href={api.ingestFilePdfUrl(file.id)} target="_blank" rel="noreferrer"
            />
          </span>
        )}
        {file.card && <span className="badge">{file.card}</span>}
        {file.template_id && <span className="badge">{file.template_id}</span>}
        {file.txn_count != null && <span className="badge">{file.txn_count} txns</span>}
        {file.confidence != null && <span className="badge">{Math.round(file.confidence * 100)}% confidence</span>}
        <span className="spacer" /><span className="sub">{open ? '▴' : '▾'}</span>
      </div>
      {open && (
        <div style={{ marginTop: 12, borderTop: '1px solid var(--border)', paddingTop: 8 }}>
          {file.steps.map((step) => <StepRow key={step.seq} step={step} />)}
          {!!file.checks.length && (
            <div style={{ marginTop: 10 }}>
              {file.checks.map((check) => (
                <div className="step-row" key={check.name}>
                  <span className="dot" style={{
                    background: COLOUR[check.passed ? 'ok' : check.severity === 'error' ? 'failed' : 'warning'],
                  }} />
                  <span className="step-name">{check.passed ? 'pass' : check.severity}</span>
                  <span className="step-detail"><b>{check.name}</b> — {check.detail}</span>
                </div>
              ))}
            </div>
          )}
          {file.error && <pre className="pipeline-error">{file.error}</pre>}
        </div>
      )}
    </div>
  )
}

/** How much of a pending statement the ledger already holds.
 *
 *  Statements overlap — a yearly download and the monthly e-statements inside
 *  it describe the same days — so "40 transactions" and "40 transactions you
 *  already have" have to read differently before anyone approves either.
 */
function Overlap({ row }: { row: Pending }) {
  const known = row.known_txn_count
  const fresh = row.new_txn_count
  if (known == null || fresh == null) return <span className="sub">—</span>
  if (!known) return <span className="sub">nothing — all {fresh} are new</span>
  if (!fresh) {
    return (
      <span className="badge" title="Approving re-files these; it adds no transactions">
        all {known} already imported
      </span>
    )
  }
  return (
    <span className="badge" style={{ color: 'var(--warning)', borderColor: 'var(--warning)' }}>
      {fresh} new · {known} already imported
    </span>
  )
}

export default function BankPipeline({
  onChanged, roster, members,
}: {
  onChanged: () => void
  roster: Member[]
  members: Set<number>
}) {
  const target = useMemo(() => importTarget(roster, members), [roster, members])
  const [uploads, setUploads] = useState<File[]>([])
  const [password, setPassword] = useState('')
  const [runs, setRuns] = useState<Run[]>([])
  const [current, setCurrent] = useState<number | null>(null)
  const [results, setResults] = useState<IngestFile[]>([])
  const [pending, setPending] = useState<Pending[]>([])
  const [picked, setPicked] = useState<Set<number>>(new Set())
  const [busy, setBusy] = useState(false)
  const [followLatest, setFollowLatest] = useState(false)
  const [sending, setSending] = useState(false)
  const [message, setMessage] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [scanSource, setScanSource] = useState<ScanSource>('mail')
  const [scanMode, setScanMode] = useState<ScanMode>('this')
  const [month, setMonth] = useState(() => new Date().toISOString().slice(0, 7))
  const [monthFrom, setMonthFrom] = useState(() => new Date().toISOString().slice(0, 7))
  const [monthTo, setMonthTo] = useState(() => new Date().toISOString().slice(0, 7))
  const [paths, setPaths] = useState('samples')
  const [accountList, setAccountList] = useState<BankAccount[]>([])
  const [scanAccounts, setScanAccounts] = useState<Set<number>>(new Set())
  const [scanUnknownAccounts, setScanUnknownAccounts] = useState(false)
  const [connections, setConnections] = useState<Mailbox[]>([])
  const [scanConnections, setScanConnections] = useState<Set<number>>(new Set())

  // The accounts to filter by, scoped to the members selected at the top. An
  // account ticked here can vanish when that selection narrows, so keep the
  // overlap and fall back to everything visible rather than an empty set.
  useEffect(() => {
    api.bankBootstrap(members).then(({ accounts }) => {
      setAccountList(accounts)
      setScanAccounts((current) => {
        const visible = accounts.map((account) => account.id)
        const kept = visible.filter((id) => current.has(id))
        return new Set(kept.length ? kept : visible)
      })
    }).catch(() => undefined)
  }, [[...members].join(',')])

  // Only mailboxes marked as carrying bank statements: a card-only connection
  // has nothing to offer this pipeline and should not be offered as a choice.
  useEffect(() => {
    api.mailboxes().then(({ mailboxes }) => {
      const usable = mailboxes.filter((box) => box.secret_ok && box.use_for_bank)
      setConnections(usable)
      setScanConnections(new Set(usable.map((box) => box.id)))
    }).catch(() => undefined)
  }, [])

  const refresh = useCallback(async () => {
    try {
      const [state, review] = await Promise.all([api.bankRuns(), api.bankPending()])
      setRuns(state.runs)
      setPending(review.pending)
      setBusy(state.busy)
      // After starting work, track the newest run until the pipeline goes idle: the
      // run row often does not exist yet the moment the request returns, and a plain
      // `selected ?? newest` would then pin the view to the *previous* run forever.
      setCurrent((selected) => (
        followLatest ? state.runs[0]?.id ?? selected : selected ?? state.runs[0]?.id ?? null
      ))
      if (!state.busy) {
        setFollowLatest(false)
        onChanged()
      }
    } catch (caught) {
      setError(String((caught as Error).message))
    }
  }, [onChanged, followLatest])

  useEffect(() => {
    setPicked((current) => {
      const available = new Set(pending.map((row) => row.id))
      const retained = [...current].filter((id) => available.has(id))
      if (retained.length) return new Set(retained)
      return new Set(pending.filter((row) => row.confidence === 1).map((row) => row.id))
    })
  }, [pending.map((row) => row.id).join(',')])

  useEffect(() => { refresh() }, [refresh])
  useEffect(() => {
    const timer = window.setInterval(refresh, busy ? 750 : 4000)
    return () => window.clearInterval(timer)
  }, [busy, refresh])
  useEffect(() => {
    if (current == null) { setResults([]); return }
    api.bankRun(current).then((detail) => setResults(detail.files))
      .catch((caught) => {
        // Drop the previous run's files rather than showing them under this one.
        setResults([])
        setError(String((caught as Error).message))
      })
  }, [current, busy])

  const upload = async () => {
    setSending(true)
    setError(null)
    setMessage(null)
    try {
      const response = await api.uploadBankStatements(uploads, password, target.id)
      setMessage(
        `${response.files.length} statement${response.files.length === 1 ? '' : 's'} uploaded `
        + `for ${target.name}. Parsing has started.`,
      )
      setUploads([])
      setBusy(true)
      setFollowLatest(true)
      await refresh()
    } catch (caught) {
      setError(String((caught as Error).message))
    } finally {
      setSending(false)
    }
  }

  const scanWindow = (): Record<string, unknown> => {
    if (scanMode === 'month') return { month }
    if (scanMode === 'range') return { month_from: monthFrom, month_to: monthTo }
    return { months: scanMode === 'last12' ? 12 : 1 }
  }

  const noScanAccounts = Boolean(
    accountList.length && scanAccounts.size === 0 && !scanUnknownAccounts,
  )
  const noScanConnections = Boolean(connections.length && scanConnections.size === 0)
  const badWindow =
    (scanMode === 'month' && !month)
    || (scanMode === 'range' && (!monthFrom || !monthTo || monthFrom > monthTo))
  const scanDisabled =
    busy || sending
    || (scanSource === 'upload'
      ? !uploads.length
      : scanSource === 'disk'
        ? !paths.trim()
        : noScanAccounts || noScanConnections || badWindow)

  const scan = async () => {
    if (scanSource === 'upload') { void upload(); return }
    setSending(true)
    setError(null)
    setMessage(null)
    try {
      if (scanSource === 'disk') {
        const response = await api.scanBankLocal({
          paths: paths.split(',').map((value) => value.trim()).filter(Boolean),
          password: password || undefined,
          member_id: target.id,
        })
        setMessage(`${response.files.length} file(s) queued for parsing.`)
      } else {
        await api.scanBankMail({
          ...scanWindow(),
          password: password || undefined,
          member_id: target.id,
          // Always explicit: an empty list means "every account in the database"
          // to the backend, which would reach past the members selected above.
          account_ids: accountList.length ? [...scanAccounts] : [],
          include_unrecognized_accounts: scanUnknownAccounts,
          connection_ids:
            scanConnections.size < connections.length ? [...scanConnections] : [],
        })
        setMessage('Searching your mailboxes for account statements.')
      }
      setBusy(true)
      setFollowLatest(true)
      await refresh()
    } catch (caught) {
      setError(String((caught as Error).message))
    } finally {
      setSending(false)
    }
  }

  const toggleConnection = (id: number) => {
    const next = new Set(scanConnections)
    next.has(id) ? next.delete(id) : next.add(id)
    setScanConnections(next)
  }

  const colourOf = (id: number) => seriesVar(
    [...accountList].sort((a, b) => a.id - b.id).findIndex((account) => account.id === id),
  )

  const toggle = (id: number) => {
    const next = new Set(picked)
    next.has(id) ? next.delete(id) : next.add(id)
    setPicked(next)
  }

  const approve = async () => {
    setError(null)
    try {
      await api.approveBankStatements([...picked], password, target.id)
      setMessage(`${picked.size} selected statement${picked.size === 1 ? '' : 's'} approved for import.`)
      setBusy(true)
      setFollowLatest(true)
      setPassword('')
      await refresh()
    } catch (caught) {
      setError(String((caught as Error).message))
    }
  }

  const reevaluate = async () => {
    setError(null)
    try {
      await api.reevaluateBankPending([], password)
      setMessage('Re-evaluating all pending bank statements with the current parser.')
      setBusy(true)
      setFollowLatest(true)
      await refresh()
    } catch (caught) {
      setError(String((caught as Error).message))
    }
  }

  const discard = async () => {
    setError(null)
    try {
      await api.discardBankPending([...picked])
      setMessage(`${picked.size} pending statement${picked.size === 1 ? '' : 's'} discarded.`)
      await refresh()
    } catch (caught) {
      setError(String((caught as Error).message))
    }
  }

  return (
    <>
      <div className="scan-bar">
        <div className="scan-stack">
          {/* Row 1 — where the statements come from. */}
          <div className="scan-row">
            <span className="sub">Source</span>
            <select
              className="select"
              value={scanSource}
              onChange={(event) => setScanSource(event.target.value as ScanSource)}
              aria-label="Where to scan account statements from"
            >
              {SCAN_SOURCES.map((source) => (
                <option key={source.value} value={source.value}>{source.label}</option>
              ))}
            </select>
          </div>

          {/* Row 2 — when to scan. An upload and a path are taken as given, so a
              statement period means nothing for them and the row is dropped. */}
          {scanSource === 'mail' && (
            <div className="scan-row">
              <span className="sub">Period</span>
              <select
                className="select"
                value={scanMode}
                onChange={(event) => setScanMode(event.target.value as ScanMode)}
                aria-label="Statement period to scan"
              >
                {SCAN_MODES.map((mode) => (
                  <option key={mode.value} value={mode.value}>{mode.label}</option>
                ))}
              </select>
              {scanMode === 'month' && (
                <span className="dates">
                  <input type="month" value={month} onChange={(event) => setMonth(event.target.value)} />
                </span>
              )}
              {scanMode === 'range' && (
                <span className="dates">
                  <input type="month" value={monthFrom} onChange={(event) => setMonthFrom(event.target.value)} />
                  <span className="sub">to</span>
                  <input type="month" value={monthTo} onChange={(event) => setMonthTo(event.target.value)} />
                </span>
              )}
              {badWindow && (
                <span className="sub" style={{ color: 'var(--critical)' }}>
                  {scanMode === 'range'
                    ? 'Pick a start month no later than the end month.'
                    : 'Pick a month.'}
                </span>
              )}
            </div>
          )}

          {/* Row 3 — what to scan: the account and mailbox filters, or the files. */}
          {scanSource === 'upload' ? (
            <div className="scan-row">
              <span className="sub">Files</span>
              <label className="upload-picker">
                <span className="btn">Choose PDFs</span>
                <input
                  type="file" accept="application/pdf,.pdf" multiple
                  onChange={(event) => setUploads(Array.from(event.target.files ?? []))}
                />
              </label>
              <span className="sub">
                {uploads.length
                  ? `${uploads.length} selected · ${uploads.map((file) => file.name).join(', ')}`
                  : 'No files selected'}
              </span>
            </div>
          ) : scanSource === 'disk' ? (
            <div className="scan-row">
              <span className="sub">File</span>
              <input
                className="input"
                value={paths}
                onChange={(event) => setPaths(event.target.value)}
                placeholder="file, folder or glob"
                aria-label="File, folder or glob to scan"
              />
              <span className="sub">Separate several with commas.</span>
            </div>
          ) : (
            <div className="scan-row">
              <span className="sub">Fetch only</span>
              <AccountSelect
                accounts={accountList}
                selected={scanAccounts}
                colourOf={colourOf}
                onChange={setScanAccounts}
                unrecognized={{
                  checked: scanUnknownAccounts, onChange: setScanUnknownAccounts,
                }}
              />
              {connections.length > 0 && (
                <details className="ms">
                  <summary className="ms-btn" style={{ cursor: 'pointer' }}>
                    {scanConnections.size === connections.length
                      ? `All connections (${connections.length})`
                      : `${scanConnections.size} of ${connections.length} connections`}
                  </summary>
                  <div className="ms-panel">
                    {connections.map((box) => (
                      <label className="ms-row" key={box.id}>
                        <input
                          type="checkbox"
                          checked={scanConnections.has(box.id)}
                          onChange={() => toggleConnection(box.id)}
                        />
                        <span>{box.address}</span>
                      </label>
                    ))}
                    <div className="ms-foot">
                      <button
                        className="link"
                        onClick={() => setScanConnections(new Set(connections.map((box) => box.id)))}
                      >
                        Select all
                      </button>
                      <button className="link" onClick={() => setScanConnections(new Set())}>
                        Clear
                      </button>
                    </div>
                  </div>
                </details>
              )}
              {noScanAccounts && (
                <span className="sub" style={{ color: 'var(--critical)' }}>
                  Select at least one account.
                </span>
              )}
              {scanUnknownAccounts && (
                <span className="sub">
                  Including accounts you have not imported yet — the mailbox search widens to
                  every known bank, so a scan takes longer.
                </span>
              )}
              {noScanConnections && (
                <span className="sub" style={{ color: 'var(--critical)' }}>
                  Select at least one connection.
                </span>
              )}
            </div>
          )}

          {/* Row 4 — the password. It opens an encrypted PDF and, for an HDFC
              smart statement, the link's password gate as well. */}
          <div className="scan-row">
            <span className="sub">Password</span>
            <input
              className="input" type="password" placeholder="statement password (optional)"
              value={password} onChange={(event) => setPassword(event.target.value)}
            />
            <span className="sub">
              Left blank, the name and date of birth saved on the Cards tab are used to derive it —
              and a password already saved for the account is always tried first.
            </span>
          </div>
        </div>

        <button className="btn primary scan-go" disabled={scanDisabled} onClick={scan}>
          {sending ? 'Working…' : busy ? 'Pipeline busy…' : '↧ Scan'}
        </button>
      </div>

      <p className="hint" style={{ marginTop: -4 }}>
        This pipeline accepts digital-text HDFC, ICICI, IndusInd and IDFC FIRST account statements.
        HDFC mails a link rather than a file — the smart statement is followed through its password
        gate and the PDF behind it is fetched. Every statement is classified, parsed and
        balance-validated first; nothing enters the bank ledger until you review the confidence and
        checks below and approve it.
      </p>
      <p className="hint">
        Statements fetched from a mailbox are filed under whoever owns that mailbox. Anything else —
        an upload, a disk scan, a statement from an unowned mailbox — is filed under{' '}
        <b>{target.name}</b>{target.explicit ? '' : ' (your default member)'}, which follows the
        member selector at the top of the page. A new account keeps that member; statements for an
        account you already have stay with whoever owns it.
      </p>
      {scanSource === 'mail' && !connections.length && (
        <div className="banner">
          No mailbox is enabled for bank statements yet — connect one on the{' '}
          <Link to="/connections">Connections</Link> tab and tick “Bank statements” for it.
          Uploading and scanning from disk work without it.
        </div>
      )}
      {message && <div className="banner upload-message">{message}</div>}
      {error && <div className="banner upload-error">{error}</div>}

      {!!pending.length && (
        <section className="card" style={{ borderColor: 'var(--warning)' }}>
          <div className="section-head">
            <div>
              <h2>
                Review {pending.length} bank statement{pending.length === 1 ? '' : 's'} before importing
              </h2>
              <p className="hint">
                The PDFs have been parsed, but no transactions below are stored yet. Statements
                that pass every validation check are selected by default.
              </p>
            </div>
            <span className="spacer" />
            <button className="link" onClick={() => setPicked(new Set(pending.map((row) => row.id)))}>
              Select all
            </button>
            <button className="link" onClick={() => setPicked(new Set())}>Clear</button>
            <button className="btn" disabled={busy} onClick={reevaluate}>
              {busy ? 'Working…' : '↻ Re-evaluate pending'}
            </button>
            <button className="btn" disabled={busy || !picked.size} onClick={discard}>
              Discard {picked.size}
            </button>
            <button className="btn primary" disabled={busy || !picked.size} onClick={approve}>
              {busy ? 'Working…' : `Import ${picked.size} selected`}
            </button>
          </div>
          <div className="tbl-wrap" style={{ marginTop: 12 }}>
            <table>
              <thead><tr>
                <th style={{ width: 34 }}></th><th>Statement</th><th>Account</th><th>Member</th>
                <th>Statement period</th><th style={{ textAlign: 'right' }}>Txns</th>
                <th>Already imported</th><th>Validation</th>
              </tr></thead>
              <tbody>
                {pending.map((row) => {
                  const passed = row.checks.filter((check) => check.passed).length
                  return (
                    <tr key={row.id}>
                      <td>
                        <input
                          type="checkbox" checked={picked.has(row.id)}
                          onChange={() => toggle(row.id)}
                        />
                      </td>
                      <td className="desc">
                        {row.filename}{row.encrypted && <> <span className="pill">encrypted</span></>}
                      </td>
                      <td>{row.card ?? 'Unknown account'}</td>
                      <td>{row.member_name ?? <span className="sub">{target.name}</span>}</td>
                      <td>{row.period_start && row.period_end ? `${row.period_start} → ${row.period_end}` : '—'}</td>
                      <td className="num">{row.txn_count ?? 0}</td>
                      <td><Overlap row={row} /></td>
                      <td>
                        <span
                          className="badge"
                          style={{
                            color: row.confidence === 1 ? 'var(--success)' : 'var(--critical)',
                            borderColor: row.confidence === 1 ? 'var(--success-fill)' : 'var(--critical)',
                          }}
                        >
                          {row.confidence == null ? 'failed' : `${Math.round(row.confidence * 100)}%`} ·{' '}
                          {passed}/{row.checks.length} checks
                        </span>
                        {row.is_duplicate && <> <span className="pill">will replace existing</span></>}
                        {!!row.checks.length && (
                          <details className="check-details">
                            <summary>show checks</summary>
                            {row.checks.map((check) => (
                              <div key={check.name} className={check.passed ? 'check-ok' : 'check-failed'}>
                                {check.passed ? '✓' : '✕'} {check.name} — {check.detail}
                              </div>
                            ))}
                          </details>
                        )}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        </section>
      )}

      <div className="pipeline-grid">
        <section className="card pipeline-runs">
          <h2>Bank pipeline history</h2>
          <p className="hint">Card-import runs are intentionally not shown here.</p>
          {!runs.length && <p className="sub">No bank statement runs yet.</p>}
          {runs.map((run) => (
            <button
              className={`run-row ${current === run.id ? 'selected' : ''}`}
              key={run.id} onClick={() => setCurrent(run.id)}
            >
              <span className="dot" style={{ background: COLOUR[run.status] ?? 'var(--text-muted)' }} />
              <span>
                <b>Run #{run.id}</b><br />
                <span className="sub">{run.started_at.replace('T', ' ')} · {run.note}</span>
              </span>
              <span className="spacer" />
              <span className="badge">
                {run.pending ? `${run.pending} awaiting approval` : `${run.ok}/${run.files} imported`}
              </span>
            </button>
          ))}
        </section>
        <section className="pipeline-detail">
          {busy && <div className="banner">A statement pipeline is running. Results refresh automatically.</div>}
          {results.map((file) => <Result key={file.id} file={file} />)}
          {current != null && !results.length && (
            <p className="empty">
              {busy ? 'Waiting for file results…' : 'This run recorded no files.'}
            </p>
          )}
        </section>
      </div>
    </>
  )
}
