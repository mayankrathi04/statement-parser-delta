import { useEffect, useMemo, useState } from 'react'
import {
  api, type Analytics as A, type Bootstrap, type CategoryFacet, type Filters, type Txn,
} from '../api'
import CardSelect from '../components/CardSelect'
import CategorySelect from '../components/CategorySelect'
import { MonthlyBars, RankBars, seriesVar } from '../components/Charts'
import { reconcileSelection, useCategoryOptions } from '../lib/categories'
import { money0, money2, monthsBefore } from '../lib/format'

const RANGES: { label: string; months: number }[] = [
  { label: '1M', months: 1 },
  { label: '4M', months: 4 },
  { label: '6M', months: 6 },
  { label: '9M', months: 9 },
  { label: '1Y', months: 12 },
  { label: 'All', months: 0 },
]

type SortKey = 'txn_date' | 'statement_month' | 'amount' | 'merchant' | 'category' | 'card'
type ForeignSortKey = 'original' | 'billed'
type PageSize = 25 | 50 | 75 | 'all'

/**
 * `resetKey` describes what the rows are — the filters, search and sort behind
 * them. Paging restarts when that changes, but not when a category edit
 * rewrites rows already on screen, which would otherwise throw the reader back
 * to page one every time they retag a transaction.
 */
function usePagination<T>(rows: T[], resetKey: unknown) {
  const [size, setSize] = useState<PageSize>(25)
  const [page, setPage] = useState(0)
  const pageSize = size === 'all' ? Math.max(rows.length, 1) : size
  const pages = Math.max(1, Math.ceil(rows.length / pageSize))
  const pageIndex = Math.min(page, pages - 1)

  useEffect(() => { setPage(0) }, [resetKey, size])

  return {
    rows: size === 'all' ? rows : rows.slice(pageIndex * pageSize, (pageIndex + 1) * pageSize),
    page: pageIndex,
    pages,
    size,
    setPage,
    setSize,
  }
}

function TablePager({
  count, page, pages, size, setPage, setSize,
}: {
  count: number
  page: number
  pages: number
  size: PageSize
  setPage: (page: number) => void
  setSize: (size: PageSize) => void
}) {
  const pageSize = size === 'all' ? Math.max(count, 1) : size
  const first = count ? page * pageSize + 1 : 0
  const last = Math.min(count, (page + 1) * pageSize)

  return (
    <div className="table-pager">
      <span className="sub">Rows {first}–{last} of {count}</span>
      <span className="spacer" />
      <label className="sub">
        Per page{' '}
        <select
          value={size}
          onChange={(e) => setSize(e.target.value === 'all' ? 'all' : Number(e.target.value) as PageSize)}
        >
          <option value={25}>25</option>
          <option value={50}>50</option>
          <option value={75}>75</option>
          <option value="all">All</option>
        </select>
      </label>
      <button className="btn" disabled={page === 0} onClick={() => setPage(page - 1)}>← Previous</button>
      <span className="sub">Page {page + 1} of {pages}</span>
      <button className="btn" disabled={page + 1 >= pages} onClick={() => setPage(page + 1)}>Next →</button>
    </div>
  )
}

export default function Analytics({ boot, members }: { boot: Bootstrap | null; members: Set<number> }) {
  const [selected, setSelected] = useState<Set<number>>(new Set())
  const [facets, setFacets] = useState<CategoryFacet[]>([])
  const [categories, setCategories] = useState<Set<string>>(new Set())
  const [range, setRange] = useState(0)
  const [from, setFrom] = useState<string | null>(null)
  const [to, setTo] = useState<string | null>(null)
  const [data, setData] = useState<A | null>(null)
  const [txns, setTxns] = useState<Txn[]>([])
  const [picked, setPicked] = useState<Set<number>>(new Set())
  const [bulkDraft, setBulkDraft] = useState('')
  const [bulkBusy, setBulkBusy] = useState(false)
  const [bulkNote, setBulkNote] = useState<string | null>(null)
  const [editingCategory, setEditingCategory] = useState<number | null>(null)
  const [categoryDraft, setCategoryDraft] = useState('')
  const [q, setQ] = useState('')
  const [sort, setSort] = useState<{ key: SortKey; dir: 1 | -1 }>({ key: 'txn_date', dir: -1 })
  const [emiAmountDir, setEmiAmountDir] = useState<1 | -1 | null>(null)
  const [foreignSort, setForeignSort] = useState<{ key: ForeignSortKey; dir: 1 | -1 } | null>(null)

  // Colour follows the card, fixed by id order, so it is stable across filters.
  const colourIndex = useMemo(() => {
    const m = new Map<number, number>()
    ;[...(boot?.cards ?? [])].sort((a, b) => a.id - b.id).forEach((c, i) => m.set(c.id, i))
    return m
  }, [boot])
  const colourOf = (id: number) => seriesVar(colourIndex.get(id) ?? 0)

  useEffect(() => {
    if (!boot) return
    setSelected(new Set(boot.cards.map((c) => c.id)))
    setFacets(boot.categories)
    setCategories(new Set(boot.categories.map((category) => category.name)))
    setFrom(boot.bounds.min)
    setTo(boot.bounds.max)
  }, [boot])

  const filters: Filters = useMemo(
    () => ({
      cards: [...selected], allCards: boot?.cards.length ?? 0, members: [...members], from, to,
      categories: [...categories], allCategories: facets.length,
    }),
    [selected, boot, members, from, to, categories, facets.length],
  )

  useEffect(() => {
    if (!boot) return
    let live = true
    Promise.all([api.analytics(filters), api.transactions(filters)]).then(([a, t]) => {
      if (live) { setData(a); setTxns(t) }
    })
    return () => { live = false }
  }, [filters, boot])

  const applyRange = (months: number) => {
    setRange(months)
    const anchor = boot?.bounds.max
    if (!months || !anchor) { setFrom(boot?.bounds.min ?? null); setTo(boot?.bounds.max ?? null) }
    else { setFrom(monthsBefore(anchor, months)); setTo(anchor) }
  }

  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase()
    const filtered = needle
      ? txns.filter((t) =>
          [t.description, t.merchant, t.category, t.card].some((v) =>
            (v ?? '').toLowerCase().includes(needle)))
      : txns
    const { key, dir } = sort
    return [...filtered].sort((a, b) => {
      const x = key === 'amount' ? a.amount : String(a[key] ?? '')
      const y = key === 'amount' ? b.amount : String(b[key] ?? '')
      return (x < y ? -1 : x > y ? 1 : 0) * dir
    })
  }, [txns, q, sort])

  useEffect(() => {
    // Selections follow the visible result set, so narrowing the search cannot
    // leave rows ticked that are no longer on screen.
    setPicked((current) => {
      if (!current.size) return current
      const visible = new Set(rows.map((row) => row.id))
      const kept = [...current].filter((id) => visible.has(id))
      return kept.length === current.size ? current : new Set(kept)
    })
  }, [rows])

  // Sourced from the bootstrap facets, not from the analytics response: those
  // are narrowed by the category filter, and the editor must keep offering the
  // labels the filter is currently hiding.
  const categoryOptions = useCategoryOptions(facets.map((c) => c.name))

  /** Re-read the filter's options after an edit moved rows between categories:
   *  a label invented in the editor has to become filterable, and the counts
   *  beside each one have to stay true. */
  const refreshFacets = async () => {
    try {
      const next = (await api.bootstrap(members)).categories
      setCategories((current) => reconcileSelection(facets, current, next))
      setFacets(next)
    } catch {
      // Leave the picker as it was: the filter still works on known labels.
    }
  }

  const applyCategory = async (ids: number[], category: string | null) => {
    if (!ids.length) return
    setBulkBusy(true)
    setBulkNote(null)
    try {
      const { rows: updated } = await api.updateCardTransactionCategories(ids, category)
      const byId = new Map(updated.map((row) => [row.id, row]))
      setTxns((current) => current.map((item) => {
        const patch = byId.get(item.id)
        return patch ? { ...item, ...patch } : item
      }))
      setData(await api.analytics(filters))
      await refreshFacets()
      setBulkNote(
        `${updated.length} transaction${updated.length === 1 ? '' : 's'} `
        + (category ? `set to ${category}.` : 'restored to their automatic category.'),
      )
      setPicked(new Set())
      setBulkDraft('')
      setEditingCategory(null)
    } catch (caught) {
      setBulkNote(String((caught as Error).message))
    } finally {
      setBulkBusy(false)
    }
  }

  const rewardRows = data?.rewards.by_month ?? []
  const emiRows = useMemo(() => {
    const source = data?.emi.rows ?? []
    return emiAmountDir == null
      ? source
      : [...source].sort((a, b) => (a.amount - b.amount) * emiAmountDir)
  }, [data?.emi.rows, emiAmountDir])
  const fcyRows = useMemo(() => {
    const source = data?.fcy.rows ?? []
    if (!foreignSort) return source
    const amount = (row: (typeof source)[number]) =>
      foreignSort.key === 'original' ? row.fcy_amount : row.amount
    return [...source].sort((a, b) => (amount(a) - amount(b)) * foreignSort.dir)
  }, [data?.fcy.rows, foreignSort])
  // Compared by value, so each table keeps its page until its own filters,
  // search or sort move.
  const rewardPager = usePagination(rewardRows, JSON.stringify(filters))
  const emiPager = usePagination(emiRows, JSON.stringify([filters, emiAmountDir]))
  const fcyPager = usePagination(fcyRows, JSON.stringify([filters, foreignSort]))
  const txnPager = usePagination(rows, JSON.stringify([filters, q, sort]))

  const head = (key: SortKey, label: string, right = false) => (
    <th
      style={{ cursor: 'pointer', textAlign: right ? 'right' : 'left' }}
      onClick={() => setSort((s) => ({ key, dir: s.key === key && s.dir === -1 ? 1 : -1 }))}
    >
      {label} {sort.key === key ? (sort.dir === -1 ? '↓' : '↑') : ''}
    </th>
  )

  if (!boot) return <p className="empty">Connecting to the API…</p>
  if (!boot.cards.length)
    return (
      <div className="banner">
        No statements yet. Import some from the <b>Pipeline</b> tab, or run{' '}
        <code>python -m sparser import samples/*.pdf --db statements.db</code>.
      </div>
    )

  const t = data?.totals
  const tiles: [string, string, string][] = t
    ? [
        ['Total spend', money0(t.spend), `${t.txn_count} transactions`],
        ['Payments & credits', money0(t.payments), 'received'],
        ['Net movement', money0(t.net), t.net >= 0 ? 'spent more than paid' : 'paid more than spent'],
        ['Average month', money0(t.avg_month), `over ${t.months} month${t.months === 1 ? '' : 's'}`],
        ['Average transaction', money0(t.avg_txn), 'per debit'],
        ['Largest transaction', money0(t.largest), 'single debit'],
      ]
    : []

  return (
    <>
      <div className="filters">
        <CardSelect cards={boot.cards} selected={selected} colourOf={colourOf} onChange={setSelected} />
        <CategorySelect categories={facets} selected={categories} onChange={setCategories} />
        {RANGES.map((r) => (
          <span
            key={r.label}
            className="chip"
            role="button"
            aria-pressed={range === r.months}
            onClick={() => applyRange(r.months)}
          >
            {r.label}
          </span>
        ))}
        <span className="dates">
          <input type="date" value={from ?? ''} onChange={(e) => { setFrom(e.target.value || null); setRange(-1) }} />
          <span>→</span>
          <input type="date" value={to ?? ''} onChange={(e) => { setTo(e.target.value || null); setRange(-1) }} />
        </span>
        <span className="spacer" />
        <a className="btn" href={api.exportUrl(filters)}>↓ JSON</a>
      </div>

      <div className="tiles">
        {tiles.map(([l, v, n]) => (
          <div className="card" key={l}>
            <div className="tile-l">{l}</div>
            <div className="tile-v">{v}</div>
            <div className="tile-n">{n}</div>
          </div>
        ))}
      </div>

      {members.size > 1 && !!data?.by_member.length && <section className="card">
        <h2>Member-wise card activity</h2>
        <p className="hint">Credits and debits remain attributable while this view combines members.</p>
        <div className="tbl-wrap"><table><thead><tr><th>Member</th><th className="num">Debits</th><th className="num">Credits</th><th className="num">Transactions</th></tr></thead>
          <tbody>{data.by_member.map((row) => <tr key={row.member_id}><td>{row.member}</td><td className="num">{money2(row.debits)}</td><td className="num cre">{money2(row.credits)}</td><td className="num">{row.n}</td></tr>)}</tbody>
        </table></div>
      </section>}

      <section className="card">
        <h2>Monthly spend and payments</h2>
        <p className="hint">
          Purchases use the transaction date; refunds and card payments use the statement cycle.
          EMI principal and conversion bookkeeping are excluded, so a purchase counts once.
        </p>
        <div className="legend">
          <span><i className="swatch" style={{ background: 'var(--s1)' }} />Spend</span>
          <span><i className="swatch" style={{ background: 'var(--s2)' }} />Payments &amp; credits</span>
        </div>
        <MonthlyBars rows={data?.monthly ?? []} />
      </section>

      <div className="grid2">
        <section className="card">
          <h2>Spend by category</h2>
          <p className="hint">Issuer-printed category where the statement gives one, else derived.</p>
          <RankBars rows={data?.by_category ?? []} />
        </section>
        <section className="card">
          <h2>Spend by card</h2>
          <p className="hint">Colour identifies the card and stays fixed as filters change.</p>
          <RankBars rows={data?.by_card ?? []} colour={(r) => colourOf(r.card_id ?? 0)} />
        </section>
      </div>

      <section className="card">
        <h2>Top merchants</h2>
        <p className="hint">Descriptions reduced to a comparable name, so outlets of one brand group together.</p>
        <RankBars rows={data?.top_merchants ?? []} />
      </section>

      {/* Reward points, EMIs and foreign-currency legs are separate ledgers from
          rupee spend — folding them into the same totals would be nonsense, so
          each gets its own section. */}
      {!!data?.rewards.by_card.length && (
        <section className="card">
          <h2>
            Reward points <span className="pill">{data.rewards.total_points.toLocaleString('en-IN')} pts</span>
          </h2>
          <p className="hint">
            Points printed on the statement, earned across {data.rewards.earning_txns} transactions.
            Programmes differ per card, so points are never summed into a rupee value.
          </p>
          <RankBars
            rows={data.rewards.by_card.map((r) => ({
              label: r.label, value: r.points, n: r.n, card_id: r.card_id,
            }))}
            colour={(r) => colourOf(r.card_id ?? 0)}
            format={(v) => `${v.toLocaleString('en-IN')} pts`}
            tipValue={(v) => `${v.toLocaleString('en-IN')} points`}
            detail={(r) => `${r.n} earning transaction${r.n === 1 ? '' : 's'}`}
            empty="No reward points in this range."
          />
          <div className="tbl-wrap tbl-scroll" style={{ marginTop: 12 }}>
            <table>
              <thead><tr><th>Month</th><th style={{ textAlign: 'right' }}>Points earned</th></tr></thead>
              <tbody>
                {rewardPager.rows.map((m) => (
                  <tr key={m.month}>
                    <td>{m.month}</td>
                    <td className="num">{m.points.toLocaleString('en-IN')}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <TablePager count={rewardRows.length} {...rewardPager} />
        </section>
      )}

      {!!data?.emi.count && (
        <section className="card">
          <h2>
            EMI transactions <span className="pill">{data.emi.count} · {money0(data.emi.total)}</span>
          </h2>
          <p className="hint">Rows the issuer flagged as converted to instalments.</p>
          <div className="tbl-wrap tbl-scroll">
            <table>
              <thead>
                <tr>
                  <th>Date</th><th>Description</th><th>Card</th>
                  <th
                    style={{ cursor: 'pointer', textAlign: 'right' }}
                    onClick={() => setEmiAmountDir((dir) => dir === -1 ? 1 : -1)}
                  >
                    Amount {emiAmountDir == null ? '' : emiAmountDir === -1 ? '↓' : '↑'}
                  </th>
                </tr>
              </thead>
              <tbody>
                {emiPager.rows.map((r, i) => (
                  <tr key={`${r.txn_date}-${r.card_id}-${r.description}-${i}`}>
                    <td>{r.txn_date}</td>
                    <td className="desc">{r.description}</td>
                    <td><i className="swatch" style={{ background: colourOf(r.card_id) }} /> {r.card}</td>
                    <td className="num">{money2(r.amount)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <TablePager count={emiRows.length} {...emiPager} />
        </section>
      )}

      {!!data?.fcy.count && (
        <section className="card">
          <h2>
            Foreign currency <span className="pill">{data.fcy.count} · {money0(data.fcy.total_inr)}</span>
          </h2>
          <p className="hint">Transactions billed in another currency, with the original amount alongside.</p>
          <div className="tbl-wrap tbl-scroll">
            <table>
              <thead>
                <tr>
                  <th>Date</th><th>Description</th><th>Card</th>
                  {(['original', 'billed'] as const).map((key) => (
                    <th
                      key={key}
                      style={{ cursor: 'pointer', textAlign: 'right' }}
                      onClick={() => setForeignSort((current) => ({
                        key,
                        dir: current?.key === key && current.dir === -1 ? 1 : -1,
                      }))}
                    >
                      {key === 'original' ? 'Original' : 'Billed'}{' '}
                      {foreignSort?.key === key ? (foreignSort.dir === -1 ? '↓' : '↑') : ''}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {fcyPager.rows.map((r, i) => (
                  <tr key={`${r.txn_date}-${r.card_id}-${r.description}-${i}`}>
                    <td>{r.txn_date}</td>
                    <td className="desc">{r.description}</td>
                    <td><i className="swatch" style={{ background: colourOf(r.card_id) }} /> {r.card}</td>
                    <td className="num">{r.currency} {r.fcy_amount.toFixed(2)}</td>
                    <td className="num">{money2(r.amount)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <TablePager count={fcyRows.length} {...fcyPager} />
        </section>
      )}

      <section className="card">
        <div style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
          <div>
            <h2>All transactions <span className="pill">{rows.length} rows</span></h2>
            <p className="hint">Every line behind the totals above. Click a heading to sort.</p>
          </div>
          <span className="spacer" />
          <input
            className="dates"
            style={{ background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 8, padding: '6px 10px', minWidth: 200 }}
            placeholder="Search description, merchant…"
            value={q}
            onChange={(e) => setQ(e.target.value)}
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
            <form className="bulk-actions" onSubmit={(event) => {
              event.preventDefault()
              if (bulkDraft.trim()) void applyCategory([...picked], bulkDraft.trim())
            }}>
              <input
                className="input" list="card-category-options" maxLength={80}
                placeholder="Set category to…" value={bulkDraft}
                onChange={(event) => setBulkDraft(event.target.value)}
                aria-label={`Category for ${picked.size} selected transactions`}
              />
              <button className="btn primary" disabled={bulkBusy || !bulkDraft.trim()}>
                {bulkBusy ? 'Applying…' : `Apply to ${picked.size}`}
              </button>
              <button type="button" className="btn" disabled={bulkBusy}
                title="Drop the manual override and use the automatic category again"
                onClick={() => void applyCategory([...picked], null)}>Automatic</button>
              <button type="button" className="btn" disabled={bulkBusy}
                onClick={() => setPicked(new Set())}>Clear</button>
            </form>
          )}
          {bulkNote && <span className="sub">{bulkNote}</span>}
        </div>

        <div className="tbl-wrap tbl-scroll bank-tbl">
          <datalist id="card-category-options">
            {categoryOptions.map((category) => <option key={category} value={category} />)}
          </datalist>
          <table>
            <thead>
              <tr>
                <th style={{ width: 30 }} aria-label="Select" />
                {head('txn_date', 'Date')}
                {head('statement_month', 'Applied statement')}
                <th>Description</th>
                {head('merchant', 'Merchant')}
                {head('category', 'Category')}
                {head('card', 'Card')}
                {head('amount', 'Amount', true)}
              </tr>
            </thead>
            <tbody>
              {txnPager.rows.map((r) => (
                <tr key={r.id} className={picked.has(r.id) ? 'row-picked' : undefined}>
                  <td>
                    <input
                      type="checkbox"
                      checked={picked.has(r.id)}
                      aria-label={`Select ${r.description}`}
                      onChange={() => setPicked((current) => {
                        const next = new Set(current)
                        next.has(r.id) ? next.delete(r.id) : next.add(r.id)
                        return next
                      })}
                    />
                  </td>
                  <td>{r.txn_date}{r.txn_time ? ` ${r.txn_time}` : ''}</td>
                  <td title={r.statement_period_start && r.statement_period_end
                    ? `${r.statement_period_start} → ${r.statement_period_end}` : undefined}>
                    {r.statement_month ?? '—'}
                  </td>
                  <td className="desc">
                    {r.description}
                    {r.is_emi && <> <span className="pill">EMI</span></>}
                    {r.fcy_currency && <> <span className="pill">{r.fcy_currency} {r.fcy_amount}</span></>}
                    {r.reward_points ? <> <span className="pill">+{r.reward_points} pts</span></> : null}
                  </td>
                  <td>{r.merchant}</td>
                  <td>
                    {editingCategory === r.id ? (
                      <form className="category-editor" onSubmit={(event) => {
                        event.preventDefault()
                        void applyCategory([r.id], categoryDraft)
                      }}>
                        <input
                          autoFocus list="card-category-options" maxLength={80}
                          value={categoryDraft}
                          onChange={(event) => setCategoryDraft(event.target.value)}
                          aria-label={`Category for ${r.description}`}
                        />
                        <button className="btn primary" disabled={bulkBusy}>Save</button>
                        {r.category_is_override && (
                          <button type="button" className="btn" disabled={bulkBusy}
                            title={`Restore automatic category: ${r.derived_category}`}
                            onClick={() => void applyCategory([r.id], null)}>Automatic</button>
                        )}
                        <button type="button" className="btn" disabled={bulkBusy}
                          onClick={() => setEditingCategory(null)}>Cancel</button>
                      </form>
                    ) : (
                      <button
                        className="category-value"
                        title={r.category_is_override
                          ? `Manually set; automatic category is ${r.derived_category}. Click to edit.`
                          : 'Automatic category. Click to edit.'}
                        onClick={() => { setEditingCategory(r.id); setCategoryDraft(r.category) }}
                      >
                        {r.category}
                        {r.category_is_override && <span className="category-edited">edited</span>}
                      </button>
                    )}
                  </td>
                  <td><i className="swatch" style={{ background: colourOf(r.card_id) }} /> {r.card}</td>
                  <td className={`num ${r.direction === 'credit' ? 'cre' : ''}`}>
                    {r.direction === 'credit' ? '+' : ''}{money2(r.amount)}
                  </td>
                </tr>
              ))}
              {!rows.length && (
                <tr><td colSpan={8} className="empty">Nothing matches these filters.</td></tr>
              )}
            </tbody>
          </table>
        </div>
        <TablePager count={rows.length} {...txnPager} />
      </section>
    </>
  )
}
