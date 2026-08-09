import { useEffect, useMemo, useState } from 'react'
import { api, type Analytics as A, type Bootstrap, type Filters, type Txn } from '../api'
import CardSelect from '../components/CardSelect'
import { MonthlyBars, RankBars, seriesVar } from '../components/Charts'
import { money0, money2, monthsBefore } from '../lib/format'

const RANGES: { label: string; months: number }[] = [
  { label: '1M', months: 1 },
  { label: '4M', months: 4 },
  { label: '6M', months: 6 },
  { label: '9M', months: 9 },
  { label: '1Y', months: 12 },
  { label: 'All', months: 0 },
]

type SortKey = 'txn_date' | 'amount' | 'merchant' | 'category' | 'card'

export default function Analytics({ boot }: { boot: Bootstrap | null }) {
  const [selected, setSelected] = useState<Set<number>>(new Set())
  const [range, setRange] = useState(0)
  const [from, setFrom] = useState<string | null>(null)
  const [to, setTo] = useState<string | null>(null)
  const [data, setData] = useState<A | null>(null)
  const [txns, setTxns] = useState<Txn[]>([])
  const [q, setQ] = useState('')
  const [sort, setSort] = useState<{ key: SortKey; dir: 1 | -1 }>({ key: 'txn_date', dir: -1 })

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
    setFrom(boot.bounds.min)
    setTo(boot.bounds.max)
  }, [boot])

  const filters: Filters = useMemo(
    () => ({ cards: [...selected], allCards: boot?.cards.length ?? 0, from, to }),
    [selected, boot, from, to],
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

      <section className="card">
        <h2>Monthly spend and payments</h2>
        <p className="hint">Debits billed to the card against payments and credits received, by month.</p>
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
          <div className="tbl-wrap" style={{ marginTop: 12 }}>
            <table>
              <thead><tr><th>Month</th><th style={{ textAlign: 'right' }}>Points earned</th></tr></thead>
              <tbody>
                {data.rewards.by_month.map((m) => (
                  <tr key={m.month}>
                    <td>{m.month}</td>
                    <td className="num">{m.points.toLocaleString('en-IN')}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}

      {!!data?.emi.count && (
        <section className="card">
          <h2>
            EMI transactions <span className="pill">{data.emi.count} · {money0(data.emi.total)}</span>
          </h2>
          <p className="hint">Rows the issuer flagged as converted to instalments.</p>
          <div className="tbl-wrap">
            <table>
              <thead>
                <tr><th>Date</th><th>Description</th><th>Card</th><th style={{ textAlign: 'right' }}>Amount</th></tr>
              </thead>
              <tbody>
                {data.emi.rows.map((r, i) => (
                  <tr key={i}>
                    <td>{r.txn_date}</td>
                    <td className="desc">{r.description}</td>
                    <td><i className="swatch" style={{ background: colourOf(r.card_id) }} /> {r.card}</td>
                    <td className="num">{money2(r.amount)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}

      {!!data?.fcy.count && (
        <section className="card">
          <h2>
            Foreign currency <span className="pill">{data.fcy.count} · {money0(data.fcy.total_inr)}</span>
          </h2>
          <p className="hint">Transactions billed in another currency, with the original amount alongside.</p>
          <div className="tbl-wrap">
            <table>
              <thead>
                <tr>
                  <th>Date</th><th>Description</th><th>Card</th>
                  <th style={{ textAlign: 'right' }}>Original</th>
                  <th style={{ textAlign: 'right' }}>Billed</th>
                </tr>
              </thead>
              <tbody>
                {data.fcy.rows.map((r, i) => (
                  <tr key={i}>
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
        <div className="tbl-wrap">
          <table>
            <thead>
              <tr>
                {head('txn_date', 'Date')}
                <th>Description</th>
                {head('merchant', 'Merchant')}
                {head('category', 'Category')}
                {head('card', 'Card')}
                {head('amount', 'Amount', true)}
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.id}>
                  <td>{r.txn_date}{r.txn_time ? ` ${r.txn_time}` : ''}</td>
                  <td className="desc">
                    {r.description}
                    {r.is_emi && <> <span className="pill">EMI</span></>}
                    {r.fcy_currency && <> <span className="pill">{r.fcy_currency} {r.fcy_amount}</span></>}
                    {r.reward_points ? <> <span className="pill">+{r.reward_points} pts</span></> : null}
                  </td>
                  <td>{r.merchant}</td>
                  <td>{r.category}</td>
                  <td><i className="swatch" style={{ background: colourOf(r.card_id) }} /> {r.card}</td>
                  <td className={`num ${r.direction === 'credit' ? 'cre' : ''}`}>
                    {r.direction === 'credit' ? '+' : ''}{money2(r.amount)}
                  </td>
                </tr>
              ))}
              {!rows.length && (
                <tr><td colSpan={6} className="empty">Nothing matches these filters.</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </section>
    </>
  )
}
