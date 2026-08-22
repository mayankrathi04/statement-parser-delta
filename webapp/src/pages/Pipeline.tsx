import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  api, type Bootstrap, type IngestFile, type Mailbox, type Member, type Pending, type Run, type Step,
} from '../api'
import CardSelect from '../components/CardSelect'
import { IconLink } from '../components/IconButton'
import { pct } from '../lib/format'
import { importTarget, type ImportTarget } from '../lib/members'

const STATUS_COLOUR: Record<string, string> = {
  ok: 'var(--success-fill)',
  approved: 'var(--success-fill)',
  running: 'var(--warning)',
  pending: 'var(--warning)',
  warning: 'var(--warning)',
  skipped: 'var(--text-muted)',
  failed: 'var(--critical)',
  done: 'var(--success-fill)',
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

/** Where the statements come from. A mailbox is searched by billing period; a
 *  path is read as given, so the period row is irrelevant and hidden for it. */
type ScanSource = 'mail' | 'disk'

/** Which billing window a mail scan covers; picks the date inputs to show. */
type ScanMode = 'this' | 'month' | 'range' | 'last12'

const SCAN_SOURCES: { value: ScanSource; label: string }[] = [
  { value: 'mail', label: 'Scan from connection' },
  { value: 'disk', label: 'Scan from file' },
]

const SCAN_MODES: { value: ScanMode; label: string }[] = [
  { value: 'this', label: 'Scan this month' },
  { value: 'month', label: 'Scan a specific month' },
  { value: 'range', label: 'Scan a month range' },
  { value: 'last12', label: 'Scan the last 12 months' },
]

function Dot({ status }: { status: string }) {
  return <span className="dot" style={{ background: STATUS_COLOUR[status] ?? 'var(--text-muted)' }} />
}

function StepRow({ step }: { step: Step }) {
  return (
    <div className="step-row">
      <Dot status={step.status} />
      <span className="step-name">{step.name}</span>
      <span className="step-detail">
        {step.detail || STEP_BLURB[step.name] || ''}
        {step.ms != null && step.ms > 0 && <span style={{ color: 'var(--baseline)' }}> · {step.ms} ms</span>}
      </span>
    </div>
  )
}

function FileCard({ file }: { file: IngestFile }) {
  const [open, setOpen] = useState(file.status !== 'ok')
  const warnings = file.checks.filter((c) => !c.passed && c.severity === 'warning')
  const passed = file.checks.filter((c) => c.passed).length

  return (
    <div className="card" style={{ marginBottom: 10 }}>
      <div className="file-head" onClick={() => setOpen((o) => !o)}>
        <Dot status={file.status} />
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
        {file.encrypted && <span className="badge">🔒 encrypted</span>}
        {file.template_id && <span className="badge">{file.template_id}</span>}
        {file.txn_count != null && <span className="badge">{file.txn_count} txns</span>}
        {file.confidence != null && (
          <span
            className="badge"
            style={{
              color: file.confidence === 1 ? 'var(--success)' : 'var(--text-secondary)',
              borderColor: file.confidence === 1 ? 'var(--success-fill)' : 'var(--border)',
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
                Validation — {passed}/{file.checks.length} passed
                {warnings.length > 0 && ` · ${warnings.length} warning${warnings.length === 1 ? '' : 's'}`}
              </div>
              {file.checks.map((c) => (
                <div className="step-row" key={c.name}>
                  <Dot status={c.passed ? 'ok' : c.severity === 'error' ? 'failed' : 'warning'} />
                  <span className="step-name">{c.passed ? 'pass' : c.severity}</span>
                  <span className="step-detail">
                    <b style={{ color: 'var(--text-secondary)' }}>{c.name}</b> — {c.detail}
                  </span>
                </div>
              ))}
            </div>
          )}
          {file.error && (
            <pre style={{ color: 'var(--critical)', fontSize: 12.5, whiteSpace: 'pre-wrap', marginTop: 10 }}>
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
  target,
}: {
  rows: Pending[]
  onImported: () => void
  target: ImportTarget
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

  const waitForPipeline = async () => {
    const deadline = Date.now() + 120_000
    while (Date.now() < deadline) {
      await new Promise((resolve) => setTimeout(resolve, 500))
      const state = await api.runs()
      if (!state.busy) return
    }
    throw new Error('The pipeline is still running after two minutes. Check the pipeline history and server log.')
  }

  const reevaluate = async () => {
    setBusy(true)
    setErr(null)
    try {
      await api.reevaluatePending()
      await waitForPipeline()
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
      await api.approve([...picked], target.id)
      // Approval starts a background job. Wait for the backend lock—not an
      // arbitrary delay—before removing the review rows. Fast imports used to
      // race the 600 ms refresh and leave this button stuck on "Importing…".
      await waitForPipeline()
      onImported()
    } catch (e) {
      setErr(String((e as Error).message))
    } finally {
      // Always, not only on failure: importing part of the list leaves the rest
      // on screen, so this list stays mounted and nothing else would ever clear
      // the flag — the buttons sat on "Importing…" for the rows still pending.
      setBusy(false)
    }
  }

  return (
    <section className="card" style={{ marginBottom: 16, borderColor: 'var(--warning)' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
        <div>
          <h2>Found {rows.length} statement{rows.length === 1 ? '' : 's'} — review before importing</h2>
          <p className="hint">
            These were downloaded and parsed but <b>nothing has been stored yet</b>. Tick what you
            want to keep. Anything already in the database is marked; importing it replaces that
            statement rather than adding a second copy. A new card is filed under the member
            shown below; one you already have stays with whoever owns it.
          </p>
        </div>
        <span className="spacer" />
        <button className="link" onClick={() => setPicked(new Set(rows.map((r) => r.id)))}>
          Select all
        </button>
        <button className="link" onClick={() => setPicked(new Set())}>Clear</button>
        <button className="btn" disabled={busy} onClick={reevaluate}>
          {busy ? 'Working…' : '↻ Re-evaluate pending'}
        </button>
        <button className="btn" disabled={busy || !picked.size} onClick={discard}>
          Discard {picked.size}
        </button>
        <button className="btn primary" disabled={busy || !picked.size} onClick={go}>
          {busy ? 'Importing…' : `Import ${picked.size} selected`}
        </button>
      </div>

      {err && <div className="banner" style={{ borderLeftColor: 'var(--critical)' }}>{err}</div>}

      <div className="tbl-wrap" style={{ marginTop: 12 }}>
        <table>
          <thead>
            <tr>
              <th style={{ width: 34 }}></th>
              <th>Statement</th>
              <th>Card</th>
              <th>Member</th>
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
                    style={{ accentColor: 'var(--series-1)', width: 15, height: 15 }}
                    checked={picked.has(r.id)}
                    onChange={() => toggle(r.id)}
                  />
                </td>
                <td className="desc">
                  {r.filename}
                  {r.encrypted && <> <span className="pill">🔒 decrypted</span></>}
                  {' '}
                  <IconLink
                    label="View PDF" icon="open"
                    title="View PDF — opens this statement in a new tab"
                    href={api.pendingPdfUrl(r.id)} target="_blank" rel="noreferrer"
                  />
                </td>
                <td>{r.card ?? <span className="sub">unknown</span>}</td>
                <td>{r.member_name ?? <span className="sub">{target.name}</span>}</td>
                <td>{r.period_start && r.period_end ? `${r.period_start} → ${r.period_end}` : '—'}</td>
                <td className="num">{r.txn_count ?? 0}</td>
                <td>
                  {r.confidence === 1 ? (
                    <span className="badge" style={{ color: 'var(--success)', borderColor: 'var(--success-fill)' }}>
                      100% — reconciled
                    </span>
                  ) : (
                    <span className="badge" style={{ color: 'var(--critical)', borderColor: 'var(--critical)' }}>
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

export default function Pipeline({
  boot, onChanged, roster, members,
}: {
  boot: Bootstrap | null
  onChanged: () => void
  roster: Member[]
  members: Set<number>
}) {
  const target = useMemo(() => importTarget(roster, members), [roster, members])
  const [runs, setRuns] = useState<Run[]>([])
  const [busy, setBusy] = useState(false)
  const [current, setCurrent] = useState<number | null>(null)
  const [files, setFiles] = useState<IngestFile[]>([])
  const [pending, setPending] = useState<Pending[]>([])
  const [msg, setMsg] = useState<string | null>(null)
  const [paths, setPaths] = useState('samples')
  const [scanSource, setScanSource] = useState<ScanSource>('mail')
  const [scanMode, setScanMode] = useState<ScanMode>('this')
  const [month, setMonth] = useState(() => new Date().toISOString().slice(0, 7))
  const [monthFrom, setMonthFrom] = useState(() => new Date().toISOString().slice(0, 7))
  const [monthTo, setMonthTo] = useState(() => new Date().toISOString().slice(0, 7))
  const [fetchCards, setFetchCards] = useState<Set<number>>(new Set())
  const [fetchUnknownCards, setFetchUnknownCards] = useState(false)
  const [connections, setConnections] = useState<Mailbox[]>([])
  const [fetchConnections, setFetchConnections] = useState<Set<number>>(new Set())

  useEffect(() => {
    if (!boot?.cards.length) return
    // `boot` is scoped to the selected members, so a card ticked here can vanish
    // when the selection narrows. Keep the overlap, and fall back to everything
    // visible rather than leaving the scan buttons disabled on an empty set.
    setFetchCards((current) => {
      const visible = boot.cards.map((card) => card.id)
      const kept = visible.filter((id) => current.has(id))
      return new Set(kept.length ? kept : visible)
    })
  }, [boot])

  // Only mailboxes marked as carrying card statements, matching what a scan
  // actually sweeps: a bank-only connection offered here would be silently
  // skipped. Not scoped to the member selector either — a mailbox is swept
  // whoever owns it, and its statements are filed under that owner.
  useEffect(() => {
    api.mailboxes().then(({ mailboxes }) => {
      const usable = mailboxes.filter((box) => box.secret_ok && box.use_for_cards)
      setConnections(usable)
      setFetchConnections(new Set(usable.map((box) => box.id)))
    }).catch(() => undefined)
  }, [])

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

  const scanOptions = (extra: Record<string, unknown>) => ({
    ...extra,
    member_id: target.id,
    // Always explicit: an empty list means "every card in the database" to the
    // backend, which would reach past the members selected at the top.
    card_ids: boot?.cards.length ? [...fetchCards] : [],
    include_unrecognized_cards: fetchUnknownCards,
    connection_ids: fetchConnections.size < connections.length ? [...fetchConnections] : [],
  })

  const noFetchCards = Boolean(boot?.cards.length && fetchCards.size === 0 && !fetchUnknownCards)
  const noFetchConnections = Boolean(connections.length && fetchConnections.size === 0)
  const badWindow =
    (scanMode === 'month' && !month) ||
    (scanMode === 'range' && (!monthFrom || !monthTo || monthFrom > monthTo))
  const scanDisabled =
    busy ||
    (scanSource === 'disk'
      ? !paths.trim()
      : noFetchCards || noFetchConnections || badWindow)

  const scanWindow = (): Record<string, unknown> => {
    if (scanMode === 'month') return { month }
    if (scanMode === 'range') return { month_from: monthFrom, month_to: monthTo }
    return { months: scanMode === 'last12' ? 12 : 1 }
  }

  const scan = () => start(() => (
    scanSource === 'disk'
      ? api.scanLocal({
          paths: paths.split(',').map((s) => s.trim()).filter(Boolean),
          member_id: target.id,
        })
      : api.scanMail(scanOptions(scanWindow()))
  ))

  const toggleConnection = (id: number) => {
    const next = new Set(fetchConnections)
    next.has(id) ? next.delete(id) : next.add(id)
    setFetchConnections(next)
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
              onChange={(e) => setScanSource(e.target.value as ScanSource)}
              aria-label="Where to scan statements from"
            >
              {SCAN_SOURCES.map((s) => (
                <option key={s.value} value={s.value}>{s.label}</option>
              ))}
            </select>
          </div>

          {/* Row 2 — when to scan. A path is read as given, so a billing window
              means nothing for a disk scan and the row is dropped entirely. The
              mode then decides which date inputs matter, so only those render. */}
          {scanSource === 'mail' && (
            <div className="scan-row">
              <span className="sub">Period</span>
              <select
                className="select"
                value={scanMode}
                onChange={(e) => setScanMode(e.target.value as ScanMode)}
                aria-label="Billing period to scan"
              >
                {SCAN_MODES.map((m) => (
                  <option key={m.value} value={m.value}>{m.label}</option>
                ))}
              </select>
              {scanMode === 'month' && (
                <span className="dates">
                  <input type="month" value={month} onChange={(e) => setMonth(e.target.value)} />
                </span>
              )}
              {scanMode === 'range' && (
                <span className="dates">
                  <input type="month" value={monthFrom} onChange={(e) => setMonthFrom(e.target.value)} />
                  <span className="sub">to</span>
                  <input type="month" value={monthTo} onChange={(e) => setMonthTo(e.target.value)} />
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

          {/* Row 3 — what to scan for: the card and mailbox filters, or the path. */}
          {scanSource === 'disk' ? (
            <div className="scan-row">
              <span className="sub">File</span>
              <input
                className="input"
                value={paths}
                onChange={(e) => setPaths(e.target.value)}
                placeholder="file, folder or glob"
                aria-label="File, folder or glob to scan"
              />
              <span className="sub">Separate several with commas.</span>
            </div>
          ) : (
            <div className="scan-row">
              <span className="sub">Fetch only</span>
              {boot && (
                <CardSelect
                  cards={boot.cards}
                  selected={fetchCards}
                  colourOf={(id) => `var(--series-${(boot.cards.findIndex((card) => card.id === id) % 8) + 1})`}
                  onChange={setFetchCards}
                  unrecognized={{ checked: fetchUnknownCards, onChange: setFetchUnknownCards }}
                />
              )}
              {connections.length > 0 && (
                <details className="ms">
                  <summary className="ms-btn" style={{ cursor: 'pointer' }}>
                    {fetchConnections.size === connections.length
                      ? `All connections (${connections.length})`
                      : `${fetchConnections.size} of ${connections.length} connections`}
                  </summary>
                  <div className="ms-panel">
                    {connections.map((box) => (
                      <label className="ms-row" key={box.id}>
                        <input
                          type="checkbox"
                          checked={fetchConnections.has(box.id)}
                          onChange={() => toggleConnection(box.id)}
                        />
                        <span>{box.address}</span>
                      </label>
                    ))}
                    <div className="ms-foot">
                      <button className="link" onClick={() => setFetchConnections(new Set(connections.map((box) => box.id)))}>
                        Select all
                      </button>
                      <button className="link" onClick={() => setFetchConnections(new Set())}>Clear</button>
                    </div>
                  </div>
                </details>
              )}
              {noFetchCards && <span className="sub" style={{ color: 'var(--critical)' }}>Select at least one card.</span>}
              {fetchUnknownCards && (
                <span className="sub">
                  Including cards you have not imported yet — the mailbox search widens to every
                  known issuer, so a scan takes longer.
                </span>
              )}
              {noFetchConnections && <span className="sub" style={{ color: 'var(--critical)' }}>Select at least one connection.</span>}
            </div>
          )}
        </div>

        {/* Centred against the whole stack, however many rows it currently has. */}
        <button className="btn primary scan-go" disabled={scanDisabled} onClick={scan}>
          {busy ? 'Working…' : '↧ Scan'}
        </button>
      </div>

      <p className="hint" style={{ marginTop: -4 }}>
        Statements fetched from a mailbox are filed under whoever owns that mailbox. Anything
        else — a disk scan, a statement from an unowned mailbox — is filed under{' '}
        <b>{target.name}</b>{target.explicit ? '' : ' (your default member)'}, which follows the
        member selector at the top of the page.
      </p>

      {msg && <div className="banner" style={{ borderLeftColor: 'var(--critical)' }}>{msg}</div>}
      {!connections.length && (
        <div className="banner">
          No mailbox is enabled for card statements yet — add your Gmail account on the{' '}
          <Link to="/connections">Connections</Link> tab and tick “Card statements” for it. Scanning
          from disk works without it.
        </div>
      )}

      {pending.length > 0 && (
        <ReviewList
          rows={pending}
          target={target}
          onImported={() => { loadRuns(); loadPending(); onChanged() }}
        />
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
