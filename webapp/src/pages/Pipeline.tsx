import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api, type Bootstrap, type IngestFile, type Pending, type Run, type Step } from '../api'
import { pct } from '../lib/format'

const STATUS_COLOUR: Record<string, string> = {
  ok: 'var(--good)',
  approved: 'var(--good)',
  running: 'var(--warn)',
  pending: 'var(--warn)',
  skipped: 'var(--muted)',
  failed: 'var(--crit)',
  done: 'var(--good)',
}

const STEP_BLURB: Record<string, string> = {
  download: 'Retrieve the PDF from the mailbox',
  decrypt: 'Unlock the password-protected document',
  classify: 'Text layer or scan? Page and character census',
  fingerprint: 'Identify the issuer and pick a template',
  extract: 'Recover transaction rows from the table geometry',
  validate: 'Reconcile the rows against the issuer’s own totals',
  store: 'Write the statement into the database',
}

function Dot({ status }: { status: string }) {
  return <span className="dot" style={{ background: STATUS_COLOUR[status] ?? 'var(--muted)' }} />
}

function StepRow({ step }: { step: Step }) {
  return (
    <div className="step-row">
      <Dot status={step.status} />
      <span className="step-name">{step.name}</span>
      <span className="step-detail">
        {step.detail || STEP_BLURB[step.name] || ''}
        {step.ms != null && step.ms > 0 && <span style={{ color: 'var(--axis)' }}> · {step.ms} ms</span>}
      </span>
    </div>
  )
}

function FileCard({ file }: { file: IngestFile }) {
  const [open, setOpen] = useState(file.status !== 'ok')
  const errors = file.checks.filter((c) => !c.passed && c.severity === 'error')

  return (
    <div className="card" style={{ marginBottom: 10 }}>
      <div className="file-head" onClick={() => setOpen((o) => !o)}>
        <Dot status={file.status} />
        <span className="file-name">{file.filename}</span>
        {file.card && <span className="badge">{file.card}</span>}
        {file.encrypted && <span className="badge">🔒 encrypted</span>}
        {file.template_id && <span className="badge">{file.template_id}</span>}
        {file.txn_count != null && <span className="badge">{file.txn_count} txns</span>}
        {file.confidence != null && (
          <span
            className="badge"
            style={{
              color: file.confidence === 1 ? 'var(--good-ink)' : 'var(--ink-2)',
              borderColor: file.confidence === 1 ? 'var(--good)' : 'var(--border)',
            }}
          >
            {pct(file.confidence)} confidence
          </span>
        )}
        <span className="spacer" />
        <span className="sub">{open ? '▴' : '▾'}</span>
      </div>

      {open && (
        <div style={{ marginTop: 12, borderTop: '1px solid var(--border)', paddingTop: 8 }}>
          {file.steps.map((s) => <StepRow key={s.seq} step={s} />)}
          {file.checks.length > 0 && (
            <div style={{ marginTop: 12 }}>
              <div className="tile-l" style={{ marginBottom: 6 }}>
                Validation — {file.checks.length - errors.length}/{file.checks.length} passed
              </div>
              {file.checks.map((c) => (
                <div className="step-row" key={c.name}>
                  <Dot status={c.passed ? 'ok' : c.severity === 'error' ? 'failed' : 'skipped'} />
                  <span className="step-name">{c.passed ? 'pass' : c.severity}</span>
                  <span className="step-detail">
                    <b style={{ color: 'var(--ink-2)' }}>{c.name}</b> — {c.detail}
                  </span>
                </div>
              ))}
            </div>
          )}
          {file.error && (
            <pre style={{ color: 'var(--crit)', fontSize: 12.5, whiteSpace: 'pre-wrap', marginTop: 10 }}>
              {file.error}
            </pre>
          )}
        </div>
      )}
    </div>
  )
}

/** Review step: nothing reaches the analytics tables until it is ticked here. */
function ReviewList({
  rows,
  onImported,
}: {
  rows: Pending[]
  onImported: () => void
}) {
  const importable = rows.filter((r) => r.confidence === 1)
  const [picked, setPicked] = useState<Set<number>>(new Set())
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    // Default to everything that reconciled; a failed parse must be opted into.
    setPicked(new Set(importable.map((r) => r.id)))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rows.map((r) => r.id).join(',')])

  const toggle = (id: number) => {
    const next = new Set(picked)
    next.has(id) ? next.delete(id) : next.add(id)
    setPicked(next)
  }

  const discard = async () => {
    setBusy(true)
    setErr(null)
    try {
      await api.discard([...picked])
      onImported()
    } catch (e) {
      setErr(String((e as Error).message))
    } finally {
      setBusy(false)
    }
  }

  const go = async () => {
    setBusy(true)
    setErr(null)
    try {
      await api.approve([...picked])
      // Approval starts a background job. Wait for the backend lock—not an
      // arbitrary delay—before removing the review rows. Fast imports used to
      // race the 600 ms refresh and leave this button stuck on "Importing…".
      const deadline = Date.now() + 120_000
      while (Date.now() < deadline) {
        await new Promise((resolve) => setTimeout(resolve, 500))
        const state = await api.runs()
        if (!state.busy) {
          onImported()
          setBusy(false)
          return
        }
      }
      throw new Error('Import is still running after two minutes. Check the pipeline history and server log.')
    } catch (e) {
      setErr(String((e as Error).message))
      setBusy(false)
    }
  }

  return (
    <section className="card" style={{ marginBottom: 16, borderColor: 'var(--warn)' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
        <div>
          <h2>Found {rows.length} statement{rows.length === 1 ? '' : 's'} — review before importing</h2>
          <p className="hint">
            These were downloaded and parsed but <b>nothing has been stored yet</b>. Tick what you
            want to keep. Anything already in the database is marked; importing it replaces that
            statement rather than adding a second copy.
          </p>
        </div>
        <span className="spacer" />
        <button className="link" onClick={() => setPicked(new Set(rows.map((r) => r.id)))}>
          Select all
        </button>
        <button className="link" onClick={() => setPicked(new Set())}>Clear</button>
        <button className="btn" disabled={busy || !picked.size} onClick={discard}>
          Discard {picked.size}
        </button>
        <button className="btn primary" disabled={busy || !picked.size} onClick={go}>
          {busy ? 'Importing…' : `Import ${picked.size} selected`}
        </button>
      </div>

      {err && <div className="banner" style={{ borderLeftColor: 'var(--crit)' }}>{err}</div>}

      <div className="tbl-wrap" style={{ marginTop: 12 }}>
        <table>
          <thead>
            <tr>
              <th style={{ width: 34 }}></th>
              <th>Statement</th>
              <th>Card</th>
              <th>Period</th>
              <th style={{ textAlign: 'right' }}>Txns</th>
              <th>Result</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.id}>
                <td>
                  <input
                    type="checkbox"
                    style={{ accentColor: 'var(--s1)', width: 15, height: 15 }}
                    checked={picked.has(r.id)}
                    onChange={() => toggle(r.id)}
                  />
                </td>
                <td className="desc">
                  {r.filename}
                  {r.encrypted && <> <span className="pill">🔒 decrypted</span></>}
                </td>
                <td>{r.card ?? <span className="sub">unknown</span>}</td>
                <td>{r.period_start && r.period_end ? `${r.period_start} → ${r.period_end}` : '—'}</td>
                <td className="num">{r.txn_count ?? 0}</td>
                <td>
                  {r.confidence === 1 ? (
                    <span className="badge" style={{ color: 'var(--good-ink)', borderColor: 'var(--good)' }}>
                      100% — reconciled
                    </span>
                  ) : (
                    <span className="badge" style={{ color: 'var(--crit)', borderColor: 'var(--crit)' }}>
                      {r.confidence != null ? pct(r.confidence) : 'failed'} — check it
                    </span>
                  )}
                  {r.is_duplicate && <> <span className="pill">already imported · will replace</span></>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  )
}

export default function Pipeline({ boot, onChanged }: { boot: Bootstrap | null; onChanged: () => void }) {
  const [runs, setRuns] = useState<Run[]>([])
  const [busy, setBusy] = useState(false)
  const [current, setCurrent] = useState<number | null>(null)
  const [files, setFiles] = useState<IngestFile[]>([])
  const [pending, setPending] = useState<Pending[]>([])
  const [msg, setMsg] = useState<string | null>(null)
  const [paths, setPaths] = useState('samples')
  const [month, setMonth] = useState(() => new Date().toISOString().slice(0, 7))

  const loadRuns = useCallback(async () => {
    const r = await api.runs().catch(() => null)
    if (!r) return
    setRuns(r.runs)
    setBusy(r.busy)
    setCurrent((c) => (c == null && r.runs.length ? r.runs[0].id : c))
  }, [])

  const loadPending = useCallback(async () => {
    const p = await api.pending().catch(() => null)
    if (p) setPending(p.pending)
  }, [])

  useEffect(() => { loadRuns(); loadPending() }, [loadRuns, loadPending])

  useEffect(() => {
    if (current == null) return
    let live = true
    const tick = async () => {
      const d = await api.run(current).catch(() => null)
      if (!live || !d) return
      setFiles(d.files)
      if (d.run.status === 'running') setTimeout(tick, 900)
      else { loadRuns(); loadPending(); onChanged() }
    }
    tick()
    return () => { live = false }
  }, [current, loadRuns, loadPending, onChanged])

  const start = async (fn: () => Promise<unknown>) => {
    setMsg(null)
    try {
      await fn()
      setBusy(true)
      setTimeout(async () => {
        const r = await api.runs()
        setRuns(r.runs)
        if (r.runs.length) setCurrent(r.runs[0].id)
      }, 400)
    } catch (e) {
      setMsg(String((e as Error).message))
    }
  }

  return (
    <>
      <div className="filters">
        <button className="btn primary" disabled={busy}
          onClick={() => start(() => api.scanMail({ months: 1 }))}>
          {busy ? 'Working…' : '↧ Scan this month'}
        </button>

        <span className="dates">
          <input type="month" value={month} onChange={(e) => setMonth(e.target.value)} />
          <button className="btn" disabled={busy || !month}
            onClick={() => start(() => api.scanMail({ month }))}>
            Scan that month
          </button>
        </span>

        <button className="btn" disabled={busy}
          onClick={() => start(() => api.scanMail({ months: 12 }))}>
          Scan last 12 months
        </button>

        <span className="spacer" />
        <input
          className="input"
          style={{ minWidth: 170 }}
          value={paths}
          onChange={(e) => setPaths(e.target.value)}
          placeholder="folder or glob"
        />
        <button className="btn" disabled={busy}
          onClick={() => start(() => api.scanLocal({ paths: paths.split(',').map((s) => s.trim()) }))}>
          Scan from disk
        </button>
      </div>

      {msg && <div className="banner" style={{ borderLeftColor: 'var(--crit)' }}>{msg}</div>}
      {boot && !boot.mailboxes_configured && (
        <div className="banner">
          No mailbox connected yet — add your Gmail accounts on the{' '}
          <Link to="/connections">Connections</Link> tab to enable fetching. Scanning from disk works
          without it.
        </div>
      )}

      {pending.length > 0 && (
        <ReviewList rows={pending} onImported={() => { loadRuns(); loadPending(); onChanged() }} />
      )}

      <div className="grid2" style={{ gridTemplateColumns: '260px 1fr', alignItems: 'start' }}>
        <section className="card">
          <h2>Runs</h2>
          <p className="hint">Every scan and import, kept for history.</p>
          {!runs.length && <p className="sub">Nothing yet.</p>}
          {runs.map((r) => (
            <div
              key={r.id}
              className="step-row"
              style={{
                cursor: 'pointer', borderRadius: 8, padding: '8px 10px',
                background: r.id === current ? 'var(--surface-2)' : undefined,
              }}
              onClick={() => setCurrent(r.id)}
            >
              <Dot status={r.status} />
              <span>
                <div style={{ fontSize: 13.5 }}>
                  {r.kind} · {r.pending ? `${r.pending} awaiting review` : `${r.ok}/${r.files} ok`}
                </div>
                <div className="step-detail">{r.started_at?.replace('T', ' ')}</div>
              </span>
            </div>
          ))}
        </section>

        <section>
          <h2 style={{ marginBottom: 4 }}>Extraction pipeline</h2>
          <p className="hint">
            Each PDF walks the same stages. A scan stops before the store step; only approved
            statements are written.
          </p>
          {!files.length && <div className="card"><p className="empty">Select or start a run.</p></div>}
          {files.map((f) => <FileCard key={f.id} file={f} />)}
        </section>
      </div>
    </>
  )
}
