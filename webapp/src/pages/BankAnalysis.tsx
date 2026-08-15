import { useEffect, useMemo, useState } from 'react'
import {
  api,
  type BankAnalytics as Analysis,
  type BankBootstrap,
  type BankFilters,
  type BankTxn,
  type CategoryFacet,
} from '../api'
import AccountSelect from '../components/AccountSelect'
import CategorySelect from '../components/CategorySelect'
import { MonthlyBars, NetBars, RankBars, seriesVar } from '../components/Charts'
import { reconcileSelection, useCategoryOptions } from '../lib/categories'
import { money0, money2, monthsBefore } from '../lib/format'

const RANGES = [
  { label: '1M', months: 1 }, { label: '4M', months: 4 },
  { label: '6M', months: 6 }, { label: '9M', months: 9 },
  { label: '1Y', months: 12 }, { label: 'All', months: 0 },
]

type SortKey =
  | 'txn_date' | 'value_date' | 'counterparty' | 'category' | 'account'
  | 'withdrawal' | 'deposit' | 'balance'

export default function BankAnalysis({ members }: { members: Set<number> }) {
  const [boot, setBoot] = useState<BankBootstrap | null>(null)
  const [selected, setSelected] = useState<Set<number>>(new Set())
  const [facets, setFacets] = useState<CategoryFacet[]>([])
  const [categories, setCategories] = useState<Set<string>>(new Set())
  const [range, setRange] = useState(0)
  const [from, setFrom] = useState<string | null>(null)
  const [to, setTo] = useState<string | null>(null)
  const [analysis, setAnalysis] = useState<Analysis | null>(null)
  const [transactions, setTransactions] = useState<BankTxn[]>([])
  const [query, setQuery] = useState('')
  const [sort, setSort] = useState<{ key: SortKey; dir: 1 | -1 }>({ key: 'txn_date', dir: -1 })
  const [page, setPage] = useState(0)
  const [error, setError] = useState<string | null>(null)
  const [editingCategory, setEditingCategory] = useState<number | null>(null)
  const [categoryDraft, setCategoryDraft] = useState('')
  const [savingCategory, setSavingCategory] = useState<number | null>(null)
  const [picked, setPicked] = useState<Set<number>>(new Set())
  const [bulkDraft, setBulkDraft] = useState('')
  const [bulkBusy, setBulkBusy] = useState(false)
  const [bulkNote, setBulkNote] = useState<string | null>(null)

  useEffect(() => {
    // Scoped to the top-bar picker, so the account chips below never offer an
    // account whose transactions the filtered analytics would exclude anyway.
    api.bankBootstrap(members).then((data) => {
      setBoot(data)
      setSelected(new Set(data.accounts.map((account) => account.id)))
      setFacets(data.categories)
      setCategories(new Set(data.categories.map((category) => category.name)))
      setFrom(data.bounds.min)
      setTo(data.bounds.max)
    }).catch((caught) => setError(String((caught as Error).message)))
  }, [members])

  const filters: BankFilters = useMemo(() => ({
    accounts: [...selected], allAccounts: boot?.accounts.length ?? 0, members: [...members], from, to,
    categories: [...categories], allCategories: facets.length,
  }), [selected, boot?.accounts.length, members, from, to, categories, facets.length])

  useEffect(() => {
    if (!boot?.accounts.length) return
    let live = true
    Promise.all([api.bankAnalytics(filters), api.bankTransactions(filters)])
      .then(([nextAnalysis, nextTransactions]) => {
        if (live) {
          setAnalysis(nextAnalysis)
          setTransactions(nextTransactions)
          setError(null)
        }
      })
      .catch((caught) => live && setError(String((caught as Error).message)))
    return () => { live = false }
  }, [boot, filters])

  const colourIndex = useMemo(() => {
    const indexes = new Map<number, number>()
    ;[...(boot?.accounts ?? [])].sort((a, b) => a.id - b.id)
      .forEach((account, index) => indexes.set(account.id, index))
    return indexes
  }, [boot])
  const colourOf = (id: number) => seriesVar(colourIndex.get(id) ?? 0)

  // Sourced from the bootstrap facets, not from the analytics response: those
  // are narrowed by the category filter, and the editor must keep offering the
  // labels the filter is currently hiding.
  const categoryOptions = useCategoryOptions(facets.map((category) => category.name))

  /** Re-read the filter's options after an edit moved rows between categories:
   *  a label invented in the editor has to become filterable, and the counts
   *  beside each one have to stay true. */
  const refreshFacets = async () => {
    try {
      const next = (await api.bankBootstrap(members)).categories
      setCategories((current) => reconcileSelection(facets, current, next))
      setFacets(next)
    } catch {
      // Leave the picker as it was: the filter still works on known labels.
    }
  }

  const saveCategory = async (row: BankTxn, category: string | null) => {
    setSavingCategory(row.id)
    try {
      const updated = await api.updateBankTransactionCategory(row.id, category)
      setTransactions((current) => current.map((item) => item.id === row.id
        ? { ...item, ...updated }
        : item))
      setAnalysis(await api.bankAnalytics(filters))
      await refreshFacets()
      setEditingCategory(null)
      setError(null)
    } catch (caught) {
      setError(String((caught as Error).message))
    } finally {
      setSavingCategory(null)
    }
  }

  /** Apply one category to every ticked row. `null` restores the automatic one. */
  const applyBulkCategory = async (category: string | null) => {
    const ids = [...picked]
    if (!ids.length) return
    setBulkBusy(true)
    setBulkNote(null)
    try {
      const { rows: updated } = await api.updateBankTransactionCategories(ids, category)
      const byId = new Map(updated.map((row) => [row.id, row]))
      setTransactions((current) => current.map((item) => {
        const patch = byId.get(item.id)
        return patch ? { ...item, ...patch } : item
      }))
      setAnalysis(await api.bankAnalytics(filters))
      await refreshFacets()
      setBulkNote(
        `${updated.length} transaction${updated.length === 1 ? '' : 's'} `
        + (category ? `set to ${category}.` : 'restored to their automatic category.'),
      )
      setPicked(new Set())
      setBulkDraft('')
      setError(null)
    } catch (caught) {
      setError(String((caught as Error).message))
    } finally {
      setBulkBusy(false)
    }
  }

  const applyRange = (months: number) => {
    setRange(months)
    const anchor = boot?.bounds.max
    if (!months || !anchor) {
      setFrom(boot?.bounds.min ?? null)
      setTo(boot?.bounds.max ?? null)
    } else {
      setFrom(monthsBefore(anchor, months))
      setTo(anchor)
    }
  }

  const rows = useMemo(() => {
    const needle = query.trim().toLowerCase()
    const filtered = needle
      ? transactions.filter((row) =>
          [row.description, row.reference, row.counterparty, row.category, row.account]
            .some((value) => (value ?? '').toLowerCase().includes(needle)))
      : transactions
    const { key, dir } = sort
    return [...filtered].sort((a, b) => {
      if (key === 'withdrawal' || key === 'deposit') {
        const direction = key === 'withdrawal' ? 'debit' : 'credit'
        const left = a.direction === direction ? a.amount : null
        const right = b.direction === direction ? b.amount : null
        if (left == null) return right == null ? 0 : 1
        if (right == null) return -1
        return (left - right) * dir
      }
      const value = (row: BankTxn): number | string => {
        if (key === 'balance') return row.balance
        return String(row[key] ?? '')
      }
      const left = value(a)
      const right = value(b)
      return (left < right ? -1 : left > right ? 1 : 0) * dir
    })
  }, [transactions, query, sort])

  useEffect(() => {
    // Selections follow the visible result set: narrowing the search must never
    // leave rows ticked that the user can no longer see or check.
    setPicked((current) => {
      if (!current.size) return current
      const visible = new Set(rows.map((row) => row.id))
      const kept = [...current].filter((id) => visible.has(id))
      return kept.length === current.size ? current : new Set(kept)
    })
  }, [rows])

  // Paging restarts when the result set is rebuilt — a new filter, search or
  // sort — but not when editing a category rewrites rows already on screen.
  useEffect(() => { setPage(0) }, [filters, query, sort])

  const pageSize = 50
  const pages = Math.max(1, Math.ceil(rows.length / pageSize))
  const pageIndex = Math.min(page, pages - 1)
  const shown = rows.slice(pageIndex * pageSize, (pageIndex + 1) * pageSize)
  const heading = (key: SortKey, label: string, right = false) => (
    <th
      style={{ cursor: 'pointer', textAlign: right ? 'right' : 'left' }}
      onClick={() => setSort((current) => ({
        key, dir: current.key === key && current.dir === -1 ? 1 : -1,
      }))}
    >
      {label} {sort.key === key ? (sort.dir === -1 ? '↓' : '↑') : ''}
    </th>
  )

  if (!boot) return <p className="empty">Connecting to the API…</p>
  if (!boot.accounts.length) {
    return <div className="banner">No bank statements yet. Upload one from the <b>Bank Pipeline</b> tab.</div>
  }

  const totals = analysis?.totals
  const tiles = totals ? [
    ['Withdrawals', money0(totals.withdrawals), `${totals.txn_count} total transactions`],
    ['Deposits', money0(totals.deposits), 'money received'],
    ['Net cash flow', money0(totals.net), totals.net >= 0 ? 'net inflow' : 'net outflow'],
    ['Opening balance', money0(totals.opening_balance), 'at the selected range'],
    ['Closing balance', money0(totals.closing_balance), 'at the selected range'],
    ['Largest withdrawal', money0(totals.largest_debit), `average ${money0(totals.avg_debit)}`],
  ] : []

  return (
    <>
      {error && <div className="banner" style={{ borderLeftColor: 'var(--crit)' }}>{error}</div>}
      <div className="filters">
        <AccountSelect
          accounts={boot.accounts} selected={selected} colourOf={colourOf} onChange={setSelected}
        />
        <CategorySelect categories={facets} selected={categories} onChange={setCategories} />
        {RANGES.map((item) => (
          <span
            key={item.label} className="chip" role="button"
            aria-pressed={range === item.months} onClick={() => applyRange(item.months)}
          >
            {item.label}
          </span>
        ))}
        <span className="dates">
          <input type="date" value={from ?? ''} onChange={(event) => {
            setFrom(event.target.value || null); setRange(-1)
          }} />
          <span>→</span>
          <input type="date" value={to ?? ''} onChange={(event) => {
            setTo(event.target.value || null); setRange(-1)
          }} />
        </span>
        <span className="spacer" />
        <a className="btn" href={api.bankExportUrl(filters)}>↓ JSON</a>
      </div>

      <div className="tiles">
        {tiles.map(([label, value, note]) => (
          <div className="card" key={label}>
            <div className="tile-l">{label}</div><div className="tile-v">{value}</div>
            <div className="tile-n">{note}</div>
          </div>
        ))}
      </div>

      {members.size > 1 && !!analysis?.by_member.length && <section className="card">
        <h2>Member-wise cash flow</h2>
        <p className="hint">Combined view, with every deposit and withdrawal still attributed.</p>
        <div className="tbl-wrap"><table><thead><tr><th>Member</th><th className="num">Withdrawals</th><th className="num">Deposits</th><th className="num">Transactions</th></tr></thead>
          <tbody>{analysis.by_member.map((row) => <tr key={row.member_id}><td>{row.member}</td><td className="num">{money2(row.withdrawals)}</td><td className="num cre">{money2(row.deposits)}</td><td className="num">{row.n}</td></tr>)}</tbody>
        </table></div>
      </section>}

      <section className="card">
        <h2>Monthly cash flow</h2>
        <p className="hint">Withdrawals and deposits are kept separate; net cash flow is deposits minus withdrawals.</p>
        <div className="legend">
          <span><i className="swatch" style={{ background: 'var(--s1)' }} />Withdrawals</span>
          <span><i className="swatch" style={{ background: 'var(--s2)' }} />Deposits</span>
        </div>
        <MonthlyBars
          rows={(analysis?.monthly ?? []).map((row) => ({
            month: row.month, spend: row.withdrawals, payments: row.deposits,
          }))}
          leftLabel="Withdrawals"
          rightLabel="Deposits"
        />
      </section>

      <div className="grid2">
        <section className="card">
          <h2>Withdrawals by category</h2>
          <p className="hint">A lightweight classification derived from each bank narration.</p>
          <RankBars rows={analysis?.by_category ?? []} empty="No withdrawals in this range." />
        </section>
        <section className="card">
          <h2>Deposits by category</h2>
          <p className="hint">Income and incoming credits, including dividends and salary.</p>
          <RankBars rows={analysis?.deposits_by_category ?? []} empty="No deposits in this range." />
        </section>
        <section className="card">
          <h2>Net by category</h2>
          <p className="hint">
            Deposits minus withdrawals. Right of the line is a net inflow, left is a net drain;
            both sides share one scale, and categories are ordered by size of effect.
          </p>
          <NetBars rows={analysis?.net_by_category ?? []} empty="No activity in this range." />
        </section>
        <section className="card">
          <h2>Withdrawals by account</h2>
          <p className="hint">Account colours remain fixed as filters change.</p>
          <RankBars
            rows={analysis?.by_account ?? []}
            colour={(row) => colourOf(row.account_id ?? 0)}
            empty="No withdrawals in this range."
          />
        </section>
      </div>

      <section className="card">
        <h2>Top withdrawal counterparties</h2>
        <p className="hint">UPI, IMPS, NEFT and other narration prefixes are reduced to a comparable name.</p>
        <RankBars rows={analysis?.top_counterparties ?? []} empty="No counterparties in this range." />
      </section>

      <section className="card">
        <div className="section-head">
          <div>
            <h2>Bank transactions <span className="pill">{rows.length} rows</span></h2>
            <p className="hint">Every parsed line, including its reference, direction and post-transaction balance.</p>
          </div>
          <span className="spacer" />
          <input
            className="input" placeholder="Search narration, reference, counterparty…"
            value={query} onChange={(event) => setQuery(event.target.value)}
          />
        </div>

        <div className="bulk-bar">
          <label className="bulk-check">
            <input
              type="checkbox"
              checked={!!rows.length && picked.size === rows.length}
              ref={(node) => {
                if (node) node.indeterminate = picked.size > 0 && picked.size < rows.length
              }}
              disabled={!rows.length}
              onChange={(event) => setPicked(
                event.target.checked ? new Set(rows.map((row) => row.id)) : new Set(),
              )}
            />
            <span>
              {picked.size
                ? `${picked.size} selected`
                : `Select all ${rows.length} result${rows.length === 1 ? '' : 's'}`}
            </span>
          </label>

          {picked.size > 0 && (
            <form
              className="bulk-actions"
              onSubmit={(event) => {
                event.preventDefault()
                if (bulkDraft.trim()) void applyBulkCategory(bulkDraft.trim())
              }}
            >
              <input
                className="input" list="bank-category-options" maxLength={80}
                placeholder="Set category to…"
                value={bulkDraft}
                onChange={(event) => setBulkDraft(event.target.value)}
                aria-label={`Category for ${picked.size} selected transactions`}
              />
              <button className="btn primary" disabled={bulkBusy || !bulkDraft.trim()}>
                {bulkBusy ? 'Applying…' : `Apply to ${picked.size}`}
              </button>
              <button
                type="button" className="btn" disabled={bulkBusy}
                title="Drop the manual override and use the automatic category again"
                onClick={() => void applyBulkCategory(null)}
              >
                Automatic
              </button>
              <button type="button" className="btn" disabled={bulkBusy}
                onClick={() => setPicked(new Set())}>
                Clear
              </button>
            </form>
          )}
          {bulkNote && <span className="sub">{bulkNote}</span>}
        </div>
        <div className="tbl-wrap tbl-scroll bank-tbl">
          <datalist id="bank-category-options">
            {categoryOptions.map((category) => <option key={category} value={category} />)}
          </datalist>
          <table>
            <thead><tr>
              <th style={{ width: 30 }} aria-label="Select" />
              {heading('txn_date', 'Date')}{heading('value_date', 'Value date')}
              <th>Narration</th><th>Reference</th>{heading('counterparty', 'Counterparty')}
              {heading('category', 'Category')}{heading('account', 'Account')}
              {heading('withdrawal', 'Withdrawal', true)}
              {heading('deposit', 'Deposit', true)}{heading('balance', 'Balance', true)}
            </tr></thead>
            <tbody>
              {shown.map((row) => (
                <tr key={row.id} className={picked.has(row.id) ? 'row-picked' : undefined}>
                  <td>
                    <input
                      type="checkbox"
                      checked={picked.has(row.id)}
                      aria-label={`Select ${row.description}`}
                      onChange={() => setPicked((current) => {
                        const next = new Set(current)
                        next.has(row.id) ? next.delete(row.id) : next.add(row.id)
                        return next
                      })}
                    />
                  </td>
                  <td>{row.txn_date}</td><td>{row.value_date ?? '—'}</td>
                  <td className="desc">{row.description}</td><td><code>{row.reference ?? '—'}</code></td>
                  <td>{row.counterparty}</td>
                  <td>
                    {editingCategory === row.id ? (
                      <form className="category-editor" onSubmit={(event) => {
                        event.preventDefault()
                        void saveCategory(row, categoryDraft)
                      }}>
                        <input
                          autoFocus list="bank-category-options" maxLength={80}
                          value={categoryDraft}
                          onChange={(event) => setCategoryDraft(event.target.value)}
                          aria-label={`Category for ${row.description}`}
                        />
                        <button className="btn primary" disabled={savingCategory === row.id}>Save</button>
                        {row.category_is_override && (
                          <button
                            type="button" className="btn" disabled={savingCategory === row.id}
                            title={`Restore automatic category: ${row.derived_category}`}
                            onClick={() => void saveCategory(row, null)}
                          >Automatic</button>
                        )}
                        <button
                          type="button" className="btn" disabled={savingCategory === row.id}
                          onClick={() => setEditingCategory(null)}
                        >Cancel</button>
                      </form>
                    ) : (
                      <button
                        className="category-value"
                        title={row.category_is_override
                          ? `Manually set; automatic category is ${row.derived_category}. Click to edit.`
                          : 'Automatically categorized from the narration. Click to edit.'}
                        onClick={() => {
                          setEditingCategory(row.id)
                          setCategoryDraft(row.category)
                        }}
                      >
                        {row.category}
                        {row.category_is_override && <span className="category-edited">edited</span>}
                      </button>
                    )}
                  </td>
                  <td><i className="swatch" style={{ background: colourOf(row.account_id) }} /> {row.account}</td>
                  <td className="num">{row.direction === 'debit' ? money2(row.amount) : '—'}</td>
                  <td className="num cre">{row.direction === 'credit' ? money2(row.amount) : '—'}</td>
                  <td className="num">{money2(row.balance)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="table-pager">
          <span className="sub">
            Rows {rows.length ? pageIndex * pageSize + 1 : 0}–{Math.min(rows.length, (pageIndex + 1) * pageSize)} of {rows.length}
          </span>
          <span className="spacer" />
          <button className="btn" disabled={pageIndex === 0} onClick={() => setPage(pageIndex - 1)}>← Previous</button>
          <span className="sub">Page {pageIndex + 1} of {pages}</span>
          <button className="btn" disabled={pageIndex + 1 >= pages} onClick={() => setPage(pageIndex + 1)}>Next →</button>
        </div>
      </section>
    </>
  )
}
