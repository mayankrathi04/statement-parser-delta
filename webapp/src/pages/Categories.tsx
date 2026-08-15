import { useCallback, useEffect, useState } from 'react'
import { api, type Category, type MajorCategory, type ProviderCategory } from '../api'

const SCOPES: { value: Category['applies_to']; label: string }[] = [
  { value: 'both', label: 'Cards & bank' },
  { value: 'cards', label: 'Cards only' },
  { value: 'bank', label: 'Bank only' },
]

const scopeLabel = (value: string) =>
  SCOPES.find((scope) => scope.value === value)?.label ?? value

/**
 * The control that does the actual filing.
 *
 * On every sub-category row, in both sections, because the question "where does
 * this land" is the one the screen exists to answer — burying it behind an edit
 * mode would make mapping forty labels forty round trips.
 */
function MajorPicker({
  category, majors, onChanged,
}: {
  category: Category
  majors: MajorCategory[]
  onChanged: () => void
}) {
  const [busy, setBusy] = useState(false)

  const pick = async (value: string) => {
    setBusy(true)
    try {
      await api.linkCategory(category.id, value ? Number(value) : null)
      onChanged()
    } finally {
      setBusy(false)
    }
  }

  return (
    <select
      className="input" disabled={busy} value={category.major_id ?? ''}
      style={category.major_id ? undefined : { borderColor: 'var(--warn, #b8860b)' }}
      onChange={(event) => void pick(event.target.value)}
    >
      <option value="">— not filed —</option>
      {majors.map((major) => (
        <option key={major.id} value={major.id}>{major.name}</option>
      ))}
    </select>
  )
}

function AddCategory({ majors, onAdded }: { majors: MajorCategory[]; onAdded: () => void }) {
  const [name, setName] = useState('')
  const [pattern, setPattern] = useState('')
  const [scope, setScope] = useState<Category['applies_to']>('both')
  const [major, setMajor] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const add = async () => {
    setBusy(true)
    setError(null)
    try {
      await api.addCategory(
        name.trim(), pattern.trim() || null, scope, major ? Number(major) : null,
      )
      setName('')
      setPattern('')
      onAdded()
    } catch (caught) {
      setError(String((caught as Error).message))
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="card">
      <h2>Add a sub-category</h2>
      <p className="hint">
        The pattern is a regular expression matched against the narration, case-insensitively.
        Leave it empty for a label you only ever apply by hand. Pick the major it belongs under
        now, or leave it unfiled and place it below.
      </p>
      <div className="form-row" style={{ marginTop: 0 }}>
        <input
          className="input" style={{ minWidth: 190 }} placeholder="Sub-category name"
          value={name} onChange={(event) => setName(event.target.value)}
        />
        <input
          className="input" style={{ minWidth: 260, fontFamily: 'ui-monospace, monospace' }}
          placeholder="Pattern — e.g. zomato|swiggy|dominos"
          value={pattern} onChange={(event) => setPattern(event.target.value)}
          onKeyDown={(event) => event.key === 'Enter' && name.trim() && add()}
        />
        <select className="input" value={scope}
          onChange={(event) => setScope(event.target.value as Category['applies_to'])}>
          {SCOPES.map((option) => (
            <option key={option.value} value={option.value}>{option.label}</option>
          ))}
        </select>
        <select className="input" value={major} onChange={(event) => setMajor(event.target.value)}>
          <option value="">— not filed —</option>
          {majors.map((row) => <option key={row.id} value={row.id}>{row.name}</option>)}
        </select>
        <button className="btn primary" disabled={busy || !name.trim()} onClick={add}>
          {busy ? 'Adding…' : 'Add sub-category'}
        </button>
      </div>
      {error && (
        <div className="banner" style={{ borderLeftColor: 'var(--crit)', marginTop: 12 }}>{error}</div>
      )}
    </section>
  )
}

/** The household's own list: headings, with what each one collects. */
function Majors({ majors, onChanged }: { majors: MajorCategory[]; onChanged: () => void }) {
  const [name, setName] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [editing, setEditing] = useState<number | null>(null)
  const [draft, setDraft] = useState('')

  const run = async (action: () => Promise<unknown>) => {
    setBusy(true)
    setError(null)
    try {
      await action()
      setEditing(null)
      onChanged()
    } catch (caught) {
      setError(String((caught as Error).message))
    } finally {
      setBusy(false)
    }
  }

  const remove = (major: MajorCategory) => {
    const count = major.children.length
    const warning = count
      ? `Delete "${major.name}"? Its ${count} sub-categor${count === 1 ? 'y goes' : 'ies go'} back to `
        + 'unfiled. The rules themselves are kept.'
      : `Delete "${major.name}"?`
    if (confirm(warning)) void run(() => api.removeMajorCategory(major.id))
  }

  return (
    <section className="card">
      <div className="section-head">
        <div>
          <h2>Major categories <span className="pill">{majors.length}</span></h2>
          <p className="hint">
            Your own short list — the one shared by name with the expense tracker, so the same
            spending reads the same way in both. A major has no pattern of its own: it collects
            whatever its sub-categories catch.
          </p>
        </div>
      </div>
      <div className="form-row" style={{ marginTop: 0 }}>
        <input
          className="input" style={{ minWidth: 220 }} placeholder="New major category"
          value={name} onChange={(event) => setName(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter' && name.trim()) {
              void run(() => api.addMajorCategory(name.trim())).then(() => setName(''))
            }
          }}
        />
        <button
          className="btn" disabled={busy || !name.trim()}
          onClick={() => void run(() => api.addMajorCategory(name.trim())).then(() => setName(''))}
        >
          Add major
        </button>
      </div>
      {error && <div className="banner" style={{ borderLeftColor: 'var(--crit)' }}>{error}</div>}
      <div className="tbl-wrap">
        <table>
          <thead>
            <tr>
              <th>Major category</th>
              <th>Collects</th>
              <th style={{ textAlign: 'right' }}>Subs</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {majors.map((major) => (
              <tr key={major.id}>
                <td>
                  {editing === major.id
                    ? <input className="input" autoFocus value={draft}
                        onChange={(event) => setDraft(event.target.value)}
                        onKeyDown={(event) => event.key === 'Enter' && draft.trim()
                          && void run(() => api.renameMajorCategory(major.id, draft.trim()))} />
                    : <b>{major.name}</b>}
                </td>
                <td className="sub">
                  {major.children.map((child) => child.name).join(', ') || '—'}
                </td>
                <td className="num">{major.children.length}</td>
                <td>
                  <span style={{ display: 'flex', gap: 12 }}>
                    {editing === major.id ? (
                      <>
                        <button className="link" disabled={busy || !draft.trim()}
                          onClick={() => void run(() => api.renameMajorCategory(major.id, draft.trim()))}>
                          save
                        </button>
                        <button className="link" onClick={() => setEditing(null)}>cancel</button>
                      </>
                    ) : (
                      <>
                        <button className="link"
                          onClick={() => { setEditing(major.id); setDraft(major.name) }}>rename</button>
                        <button className="link" disabled={busy}
                          onClick={() => remove(major)}>delete</button>
                      </>
                    )}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="hint" style={{ marginTop: 14, marginBottom: 0 }}>
        Renaming a major moves no transactions — no row ever carries its name, only the
        sub-category's.
      </p>
    </section>
  )
}

/** What is still uncounted, at the top of the page rather than buried. */
function Unfiled({
  unmapped, majors, onChanged,
}: {
  unmapped: Category[]
  majors: MajorCategory[]
  onChanged: () => void
}) {
  if (!unmapped.length) return null
  return (
    <section className="card" style={{ borderLeftColor: 'var(--warn, #b8860b)', borderLeftWidth: 3 }}>
      <h2>Not filed under a major <span className="pill">{unmapped.length}</span></h2>
      <p className="hint">
        These still categorise transactions exactly as they always did — but they roll up into
        nothing, so any total grouped by major leaves them out. They are listed here rather than
        swept into an "Other" heading, because a category quietly absorbed into a bucket looks
        filed and nobody goes looking for it again.
      </p>
      <div className="tbl-wrap">
        <table>
          <thead>
            <tr>
              <th>Sub-category</th><th style={{ width: 220 }}>File under</th>
              <th style={{ textAlign: 'right' }}>Card rows</th>
              <th style={{ textAlign: 'right' }}>Bank rows</th>
            </tr>
          </thead>
          <tbody>
            {unmapped.map((category) => (
              <tr key={category.id}>
                <td><b>{category.name}</b></td>
                <td><MajorPicker category={category} majors={majors} onChanged={onChanged} /></td>
                {/* Optional: a payload missing the counts should cost a number,
                    not the whole page — a render throw here unmounts the tree. */}
                <td className="num">{category.usage?.cards ?? 0}</td>
                <td className="num">{category.usage?.bank ?? 0}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  )
}

function CategoryRow({
  category, index, total, majors, onChanged,
}: {
  category: Category
  index: number
  total: number
  majors: MajorCategory[]
  onChanged: (reorder?: number[]) => void
}) {
  const [editing, setEditing] = useState(false)
  const [name, setName] = useState(category.name)
  const [pattern, setPattern] = useState(category.pattern ?? '')
  const [scope, setScope] = useState(category.applies_to)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  const used = category.usage.cards + category.usage.bank

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

  const save = () => run(async () => {
    const updated = await api.updateCategory(category.id, {
      name: name.trim(),
      pattern: pattern.trim() || null,
      clear_pattern: !pattern.trim(),
      applies_to: scope,
    })
    // A rename carries its transactions with it; say how many so it is obvious
    // the analysis pages have moved too.
    if (updated.moved) {
      setNotice(`Renamed — ${updated.moved} transaction${updated.moved === 1 ? '' : 's'} moved with it.`)
    }
    return updated
  })

  const remove = () => {
    const warning = used
      ? `Delete "${category.name}"? ${used} transaction${used === 1 ? '' : 's'} currently show it. `
        + 'They keep their data and fall back to another rule when you re-apply.'
      : `Delete "${category.name}"?`
    if (confirm(warning)) void run(() => api.removeCategory(category.id))
  }

  return (
    <>
      <tr>
        <td>
          <span className="order-buttons">
            <button className="link" disabled={busy || index === 0} title="Move up"
              onClick={() => onChanged([index, index - 1])}>↑</button>
            <button className="link" disabled={busy || index === total - 1} title="Move down"
              onClick={() => onChanged([index, index + 1])}>↓</button>
          </span>
        </td>
        <td>
          {editing
            ? <input className="input" autoFocus value={name}
                onChange={(event) => setName(event.target.value)} />
            : <b>{category.name}</b>}
        </td>
        <td>
          {editing ? (
            <input
              className="input" style={{ minWidth: 260, fontFamily: 'ui-monospace, monospace' }}
              value={pattern} placeholder="no pattern — manual only"
              onChange={(event) => setPattern(event.target.value)}
              onKeyDown={(event) => event.key === 'Enter' && name.trim() && save()}
            />
          ) : category.pattern ? (
            <code className="pattern">{category.pattern}</code>
          ) : (
            <span className="sub">manual only</span>
          )}
        </td>
        <td>
          {editing ? (
            <select className="input" value={scope}
              onChange={(event) => setScope(event.target.value as Category['applies_to'])}>
              {SCOPES.map((option) => (
                <option key={option.value} value={option.value}>{option.label}</option>
              ))}
            </select>
          ) : <span className="pill">{scopeLabel(category.applies_to)}</span>}
        </td>
        <td>
          <MajorPicker category={category} majors={majors} onChanged={() => onChanged()} />
        </td>
        <td className="num">{category.usage.cards}</td>
        <td className="num">{category.usage.bank}</td>
        <td>
          <span style={{ display: 'flex', gap: 12, flexWrap: 'wrap' }}>
            {editing ? (
              <>
                <button className="link" disabled={busy || !name.trim()} onClick={save}>save</button>
                <button className="link" onClick={() => {
                  setEditing(false)
                  setName(category.name)
                  setPattern(category.pattern ?? '')
                  setScope(category.applies_to)
                }}>cancel</button>
              </>
            ) : (
              <>
                <button className="link" onClick={() => setEditing(true)}>edit</button>
                <button className="link" disabled={busy} onClick={remove}>delete</button>
              </>
            )}
          </span>
        </td>
      </tr>
      {(error || notice) && (
        <tr><td colSpan={8}>
          <div className="banner" style={error ? { borderLeftColor: 'var(--crit)' } : undefined}>
            {error ?? notice}
          </div>
        </td></tr>
      )}
    </>
  )
}

export default function Categories() {
  const [categories, setCategories] = useState<Category[]>([])
  const [majors, setMajors] = useState<MajorCategory[]>([])
  const [unmapped, setUnmapped] = useState<Category[]>([])
  const [provider, setProvider] = useState<ProviderCategory[]>([])
  const [error, setError] = useState<string | null>(null)
  const [note, setNote] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const load = useCallback(async () => {
    try {
      const data = await api.categories()
      setCategories(data.categories)
      setMajors(data.majors)
      setUnmapped(data.unmapped)
      setProvider(data.provider_categories)
      setError(null)
    } catch (caught) {
      setError(String((caught as Error).message))
    }
  }, [])

  useEffect(() => { void load() }, [load])

  /** Swap two positions and persist the whole order — first match wins, so it matters. */
  const swap = async (from: number, to: number) => {
    const ids = categories.map((category) => category.id)
    ;[ids[from], ids[to]] = [ids[to], ids[from]]
    setCategories((current) => {
      const next = [...current]
      ;[next[from], next[to]] = [next[to], next[from]]
      return next
    })
    try {
      await api.reorderCategories(ids)
    } catch (caught) {
      setError(String((caught as Error).message))
      void load()
    }
  }

  const reapply = async () => {
    setBusy(true)
    setNote(null)
    try {
      const result = await api.reapplyCategories()
      setNote(
        `Re-applied: ${result.cards} card and ${result.bank} bank transaction`
        + `${result.cards + result.bank === 1 ? '' : 's'} re-categorised. `
        + 'Manually set rows were left as they are.',
      )
      await load()
    } catch (caught) {
      setError(String((caught as Error).message))
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      {error && <div className="banner" style={{ borderLeftColor: 'var(--crit)' }}>{error}</div>}

      <Majors majors={majors} onChanged={load} />

      <Unfiled unmapped={unmapped} majors={majors} onChanged={load} />

      <AddCategory majors={majors} onAdded={load} />

      <section className="card">
        <div className="section-head">
          <div>
            <h2>Sub-categories <span className="pill">{categories.length}</span></h2>
            <p className="hint">
              One list, shared by the card and bank ledgers, so a category means the same thing
              everywhere. Rules are tried top down and <b>the first match wins</b> — put the
              specific ones above the general ones. Each one rolls up into the major you file
              it under.
            </p>
          </div>
          <span className="spacer" />
          <button className="btn primary" disabled={busy} onClick={reapply}>
            {busy ? 'Re-applying…' : 'Re-apply to all transactions'}
          </button>
        </div>
        {note && <div className="banner">{note}</div>}
        <div className="tbl-wrap">
          <table>
            <thead>
              <tr>
                <th style={{ width: 54 }}>Order</th>
                <th>Sub-category</th><th>Pattern</th><th>Applies to</th>
                <th style={{ width: 200 }}>Major</th>
                <th style={{ textAlign: 'right' }}>Card rows</th>
                <th style={{ textAlign: 'right' }}>Bank rows</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {categories.map((category, index) => (
                <CategoryRow
                  key={category.id}
                  category={category}
                  index={index}
                  total={categories.length}
                  majors={majors}
                  onChanged={(move) => move ? void swap(move[0], move[1]) : void load()}
                />
              ))}
            </tbody>
          </table>
        </div>
        <p className="hint" style={{ marginTop: 14, marginBottom: 0 }}>
          Your rules take priority over the category a card issuer printed. A category you set on
          a single transaction by hand always wins, and re-applying never overwrites it.
        </p>
      </section>

      <section className="card">
        <h2>From your card issuers <span className="pill">not filed</span></h2>
        <p className="hint">
          These arrive printed on the statement — every issuer mints its own, which is where an
          endless category list comes from. Filing one adopts it as a sub-category with{' '}
          <b>no pattern</b>, so it keeps categorising exactly the rows the issuer already put in
          it and never competes with your own rules. Give it a pattern later if you want it to
          claim more.
        </p>
        {!provider.length && (
          <p className="sub">
            No issuer-supplied categories in your data — every category shown in the app is one of yours.
          </p>
        )}
        {!!provider.length && (
          <div className="tbl-wrap">
            <table>
              <thead>
                <tr>
                  <th>Category</th><th>Printed by</th>
                  <th style={{ textAlign: 'right' }}>Card rows</th>
                  <th style={{ width: 220 }}>File under</th>
                </tr>
              </thead>
              <tbody>
                {provider.map((row) => (
                  <tr key={row.name}>
                    <td>{row.name}</td>
                    <td className="sub">{row.sources.filter(Boolean).join(', ') || '—'}</td>
                    <td className="num">{row.rows}</td>
                    <td>
                      <select
                        className="input" value=""
                        onChange={(event) => {
                          if (!event.target.value) return
                          void api
                            .addCategory(row.name, null, 'cards', Number(event.target.value))
                            .then(load)
                            .catch((caught) => setError(String((caught as Error).message)))
                        }}
                      >
                        <option value="">— choose a major —</option>
                        {majors.map((major) => (
                          <option key={major.id} value={major.id}>{major.name}</option>
                        ))}
                      </select>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </>
  )
}
