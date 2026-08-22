import { useCallback, useEffect, useState } from 'react'
import { api, type ScanDefaults as Defaults } from '../api'

/**
 * The mailbox search a scan falls back to.
 *
 * Every card and account that has no rules of its own is searched with these —
 * which is every *unrecognized* one, since only an import can create a saved
 * rule. Without this panel that filter is invisible: you tick "Unrecognized
 * cards", the scan finds nothing, and there is no way to see what it looked for.
 */
export default function ScanDefaults({ kind }: { kind: 'cards' | 'bank' }) {
  const noun = kind === 'cards' ? 'card' : 'account'
  const [defaults, setDefaults] = useState<Defaults | null>(null)
  const [senders, setSenders] = useState('')
  const [subjects, setSubjects] = useState('')
  const [busy, setBusy] = useState(false)
  const [saved, setSaved] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const apply = useCallback((next: Defaults) => {
    setDefaults(next)
    setSenders(next.senders.join('\n'))
    setSubjects(next.subjects.join('\n'))
  }, [])

  const load = useCallback(async () => {
    try {
      apply(await api.scanDefaults(kind))
      setError(null)
    } catch (caught) {
      setError(String((caught as Error).message))
    }
  }, [kind, apply])

  useEffect(() => { load() }, [load])

  const lines = (value: string) => value.split(/[\n,]/).map((v) => v.trim()).filter(Boolean)

  const save = async () => {
    setBusy(true)
    setSaved(false)
    setError(null)
    try {
      apply(await api.saveScanDefaults(kind, lines(senders), lines(subjects)))
      setSaved(true)
    } catch (caught) {
      setError(String((caught as Error).message))
    } finally {
      setBusy(false)
    }
  }

  const reset = async () => {
    if (!confirm(`Restore the sender and subject lists this project ships with?`)) return
    setBusy(true)
    setSaved(false)
    setError(null)
    try {
      apply(await api.resetScanDefaults(kind))
    } catch (caught) {
      setError(String((caught as Error).message))
    } finally {
      setBusy(false)
    }
  }

  const edited = defaults
    && (lines(senders).join('|') !== defaults.senders.join('|')
      || lines(subjects).join('|') !== defaults.subjects.join('|'))

  return (
    <section className="card">
      <div className="section-head">
        <div>
          <h2>What a scan searches for unrecognized {noun}s</h2>
          <p className="hint" style={{ marginBottom: 0 }}>
            A {noun} you have already imported can carry its own sender and subject rules —
            set them by expanding its row above. Everything else is searched with the lists
            below: every {noun} you have <b>not</b> imported yet, and any saved {noun} whose
            own rules are incomplete. So this is the filter behind the
            “Unrecognized {noun}s” option in the pipeline — if a scan finds nothing, this is
            where to look first.
          </p>
        </div>
        <span className="spacer" />
        {defaults?.customised
          ? <span className="pill" title="Edited from what the project ships with">edited</span>
          : <span className="sub">using the built-in lists</span>}
      </div>

      <div className="mail-rules-grid" style={{ marginTop: 12 }}>
        <label className="mail-rule-field">
          <span className="sub">
            Sender IDs — {defaults?.senders.length ?? 0} in use, matched against the From header
          </span>
          <textarea
            className="input mail-rule-input"
            rows={9}
            placeholder={kind === 'cards' ? 'hdfcbank.net\nsbicard.com' : 'hdfcbank.net\nicicibank.com'}
            value={senders}
            onChange={(event) => { setSenders(event.target.value); setSaved(false) }}
          />
        </label>
        <label className="mail-rule-field">
          <span className="sub">
            Subject phrases — {defaults?.subjects.length ?? 0} in use, searched independently
            of the sender
          </span>
          <textarea
            className="input mail-rule-input"
            rows={9}
            placeholder={kind === 'cards' ? 'credit card statement' : 'account statement\nsmart statement'}
            value={subjects}
            onChange={(event) => { setSubjects(event.target.value); setSaved(false) }}
          />
        </label>
      </div>

      <p className="hint">
        One per line. A domain matches any address at it. Gmail finds mail matching
        <b> any sender or any subject</b> in these lists, and the classifier then decides
        whether what it found really is {kind === 'cards' ? 'a card' : 'an account'} statement —
        so a phrase that is too broad costs time, not correctness. Clearing a box restores that
        list to the built-in one; a scan is never left with nothing to search.
      </p>

      <div className="mail-rule-actions">
        <button className="btn primary" disabled={busy || !edited} onClick={save}>
          {busy ? 'Saving…' : 'Save search lists'}
        </button>
        <button className="btn" disabled={busy || !defaults?.customised} onClick={reset}>
          Restore built-in lists
        </button>
        {saved && <span className="sub">saved</span>}
        {edited && !saved && <span className="sub">unsaved changes</span>}
        {error && <span className="mail-rule-error">{error}</span>}
      </div>
    </section>
  )
}
