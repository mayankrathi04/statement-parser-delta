import { useCallback, useEffect, useMemo, useState } from 'react'
import { api, type IngestFile, type Member, type Pending, type Run, type Step } from '../api'
import { importTarget } from '../lib/members'

const COLOUR: Record<string, string> = {
  ok: 'var(--good)', done: 'var(--good)', failed: 'var(--crit)',
  skipped: 'var(--axis)', pending: 'var(--warn)', warning: 'var(--warn)', running: 'var(--warn)',
}

function StepRow({ step }: { step: Step }) {
  return (
    <div className="step-row">
      <span className="dot" style={{ background: COLOUR[step.status] ?? 'var(--muted)' }} />
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
        <span className="dot" style={{ background: COLOUR[file.status] ?? 'var(--muted)' }} />
        <span className="file-name">{file.filename}</span>
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
      <section className="card">
        <h2>Upload bank statements</h2>
        <p className="hint">
          This pipeline accepts digital-text HDFC, ICICI, IndusInd and IDFC FIRST
          account-statement PDFs. Files are classified,
          parsed and balance-validated first. Nothing enters the bank ledger until you review the
          confidence and checks below and explicitly approve it.
        </p>
        <div className="upload-row">
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
        <div className="form-row">
          <input
            className="input" type="password" placeholder="PDF password (optional)"
            value={password} onChange={(event) => setPassword(event.target.value)}
          />
          <button
            className="btn primary" disabled={!uploads.length || sending || busy} onClick={upload}
          >
            {sending ? 'Uploading…' : busy ? 'Pipeline busy…' : 'Upload and review'}
          </button>
        </div>
        <p className="hint" style={{ marginBottom: 0 }}>
          Filing under <b>{target.name}</b>
          {target.explicit
            ? ' — the member selected at the top of the page.'
            : ' (your default member). To file these under someone else, pick that one member'
              + ' in the selector at the top of the page before uploading.'}
          {' '}A new account keeps this member; statements for an account you already
          have stay with whoever owns it.
        </p>
        {message && <div className="banner upload-message">{message}</div>}
        {error && <div className="banner upload-error">{error}</div>}
      </section>

      {!!pending.length && (
        <section className="card" style={{ borderColor: 'var(--warn)' }}>
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
                <th>Statement period</th><th style={{ textAlign: 'right' }}>Txns</th><th>Validation</th>
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
                      <td>
                        <span
                          className="badge"
                          style={{
                            color: row.confidence === 1 ? 'var(--good-ink)' : 'var(--crit)',
                            borderColor: row.confidence === 1 ? 'var(--good)' : 'var(--crit)',
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
              <span className="dot" style={{ background: COLOUR[run.status] ?? 'var(--muted)' }} />
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
