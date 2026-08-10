import { useEffect, useMemo, useState } from 'react'
import {
  api,
  type BankAnalytics as Analysis,
  type BankBootstrap,
  type BankFilters,
  type BankTxn,
} from '../api'
import AccountSelect from '../components/AccountSelect'
import { MonthlyBars, RankBars, seriesVar } from '../components/Charts'
import { money0, money2, monthsBefore } from '../lib/format'

const RANGES = [
  { label: '1M', months: 1 }, { label: '4M', months: 4 },
  { label: '6M', months: 6 }, { label: '9M', months: 9 },
  { label: '1Y', months: 12 }, { label: 'All', months: 0 },
]

const DEFAULT_CATEGORIES = [
  'Dividends', 'Salary & Income', 'Investments', 'Loans & EMI', 'Cash',
  'Transfers', 'Food & Dining', 'Shopping', 'Travel', 'Bills & Utilities',
  'Healthcare', 'Entertainment', 'Education', 'Fees & Charges', 'Tax', 'Other',
]

type SortKey =
  | 'txn_date' | 'value_date' | 'counterparty' | 'category' | 'account'
  | 'withdrawal' | 'deposit' | 'balance'

export default function BankAnalysis() {
  const [boot, setBoot] = useState<BankBootstrap | null>(null)
  const [selected, setSelected] = useState<Set<number>>(new Set())
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

  useEffect(() => {
    api.bankBootstrap().then((data) => {
      setBoot(data)
      setSelected(new Set(data.accounts.map((account) => account.id)))
      setFrom(data.bounds.min)
      setTo(data.bounds.max)
    }).catch((caught) => setError(String((caught as Error).message)))
  }, [])

  const filters: BankFilters = useMemo(() => ({
    accounts: [...selected], allAccounts: boot?.accounts.length ?? 0, from, to,
  }), [selected, boot?.accounts.length, from, to])

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

  const categoryOptions = useMemo(() => [...new Set([
    ...DEFAULT_CATEGORIES,
    ...(analysis?.by_category ?? []).map((row) => row.label),
    ...(analysis?.deposits_by_category ?? []).map((row) => row.label),
  ])].sort(), [analysis])

  const saveCategory = async (row: BankTxn, category: string | null) => {
    setSavingCategory(row.id)
    try {
      const updated = await api.updateBankTransactionCategory(row.id, category)
      setTransactions((current) => current.map((item) => item.id === row.id
        ? { ...item, ...updated }
        : item))
      setAnalysis(await api.bankAnalytics(filters))
      setEditingCategory(null)
      setError(null)
    } catch (caught) {
      setError(String((caught as Error).message))
    } finally {
      setSavingCategory(null)
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

  useEffect(() => { setPage(0) }, [rows])
  const pageSize = 50
  const pages = Math.max(1, Math.ceil(rows.length / pageSize))
  const shown = rows.slice(page * pageSize, (page + 1) * pageSize)
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
        <div className="tbl-wrap tbl-scroll bank-tbl">
          <datalist id="bank-category-options">
            {categoryOptions.map((category) => <option key={category} value={category} />)}
          </datalist>
          <table>
            <thead><tr>
              {heading('txn_date', 'Date')}{heading('value_date', 'Value date')}
              <th>Narration</th><th>Reference</th>{heading('counterparty', 'Counterparty')}
              {heading('category', 'Category')}{heading('account', 'Account')}
              {heading('withdrawal', 'Withdrawal', true)}
              {heading('deposit', 'Deposit', true)}{heading('balance', 'Balance', true)}
            </tr></thead>
            <tbody>
              {shown.map((row) => (
                <tr key={row.id}>
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
            Rows {rows.length ? page * pageSize + 1 : 0}–{Math.min(rows.length, (page + 1) * pageSize)} of {rows.length}
          </span>
          <span className="spacer" />
          <button className="btn" disabled={page === 0} onClick={() => setPage(page - 1)}>← Previous</button>
          <span className="sub">Page {page + 1} of {pages}</span>
          <button className="btn" disabled={page + 1 >= pages} onClick={() => setPage(page + 1)}>Next →</button>
        </div>
      </section>
    </>
  )
}
