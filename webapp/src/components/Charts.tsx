import { useState, type ReactNode } from 'react'
import type { Slice } from '../api'
import { money0, money2 } from '../lib/format'

/* Categorical slots in fixed order — a series' colour follows the entity, never
   its rank, so filtering never repaints the survivors. */
export const SERIES = ['--s1', '--s2', '--s3', '--s4', '--s5', '--s6', '--s7', '--s8']
export const seriesVar = (i: number) => `var(${SERIES[i % SERIES.length]})`

type TipState = { html: ReactNode; x: number; y: number } | null

export function Tooltip({ tip }: { tip: TipState }) {
  if (!tip) return null
  return (
    <div
      className="tip"
      style={{ left: Math.min(tip.x + 14, window.innerWidth - 300), top: tip.y + 14 }}
    >
      {tip.html}
    </div>
  )
}

export function useTip() {
  const [tip, setTip] = useState<TipState>(null)
  const bind = (html: ReactNode) => ({
    onMouseMove: (e: React.MouseEvent) => setTip({ html, x: e.clientX, y: e.clientY }),
    onMouseLeave: () => setTip(null),
  })
  return { tip, bind }
}

/** Horizontal ranking bars, built in HTML rather than SVG.
 *
 *  An SVG with a viewBox scales its text along with the drawing, so the same
 *  component renders 12px labels in a half-width column and oversized ones at
 *  full width. Laying the rows out in HTML keeps type at a constant size, lets
 *  CSS truncate long merchant names, and reflows properly on narrow screens.
 *
 *  Values are always labelled: three light-mode series colours sit below 3:1 on
 *  the surface, and the palette's relief rule requires visible labels wherever
 *  that happens.
 */
export function RankBars({
  rows,
  colour,
  format = money0,
  detail = (r) => `${r.n} transaction${r.n === 1 ? '' : 's'}`,
  tipValue = money2,
  empty = 'No spending in this range.',
}: {
  rows: Slice[]
  colour?: (r: Slice, i: number) => string
  format?: (v: number) => string
  detail?: (r: Slice) => string
  tipValue?: (v: number) => string
  empty?: string
}) {
  const { tip, bind } = useTip()
  if (!rows.length) return <p className="empty">{empty}</p>

  const max = Math.max(...rows.map((r) => r.value)) || 1

  return (
    <>
      <div className="rank">
        {rows.map((r, i) => (
          <div
            className="rank-row"
            key={r.label + i}
            {...bind(
              <>
                <div>{r.label}</div>
                <b>{tipValue(r.value)}</b>
                <div style={{ color: 'var(--muted)' }}>{detail(r)}</div>
              </>,
            )}
          >
            <span className="rank-label" title={r.label}>{r.label}</span>
            <span className="rank-track">
              <span
                className="rank-fill"
                style={{
                  width: `${Math.max(1.5, (r.value / max) * 100)}%`,
                  background: colour ? colour(r, i) : 'var(--s1)',
                }}
              />
            </span>
            <span className="rank-value">{format(r.value)}</span>
          </div>
        ))}
      </div>
      <Tooltip tip={tip} />
    </>
  )
}

function niceStep(max: number): number {
  const raw = max / 4
  const mag = Math.pow(10, Math.floor(Math.log10(raw)))
  return [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) ?? mag * 10
}

/** Grouped bars: spend and payments share one axis because both are rupees.
 *  Two y-scales would be a dual-axis chart, which is never correct. */
export function MonthlyBars({
  rows,
  leftLabel = 'Spend',
  rightLabel = 'Payments',
}: {
  rows: { month: string; spend: number; payments: number }[]
  leftLabel?: string
  rightLabel?: string
}) {
  const { tip, bind } = useTip()
  if (!rows.length) return <p className="empty">No data in this range.</p>

  const W = 980, H = 300, padL = 66, padR = 12, padT = 14, padB = 40
  const iw = W - padL - padR
  const ih = H - padT - padB
  const max = Math.max(...rows.flatMap((r) => [r.spend, r.payments])) || 1
  const step = niceStep(max)
  const top = Math.ceil(max / step) * step
  const slot = iw / rows.length
  const bw = Math.min(26, slot / 2.9)
  const y = (v: number) => padT + ih - (v / top) * ih

  const ticks: number[] = []
  for (let v = 0; v <= top + 1; v += step) ticks.push(v)

  const fmtMonth = (m: string, i: number) => {
    const [yy, mm] = m.split('-')
    const name = new Date(Number(yy), Number(mm) - 1).toLocaleString('en', { month: 'short' })
    return mm === '01' || i === 0 ? `${name} '${yy.slice(2)}` : name
  }

  return (
    <>
      <svg viewBox={`0 0 ${W} ${H}`} role="img">
        {ticks.map((v) => (
          <g key={v}>
            <line className="gl" x1={padL} x2={W - padR} y1={y(v)} y2={y(v)} />
            <text className="ax" x={padL - 10} y={y(v) + 4} textAnchor="end">
              {v ? money0(v) : '0'}
            </text>
          </g>
        ))}
        {rows.map((r, i) => {
          const cx = padL + slot * i + slot / 2
          const hS = r.spend ? Math.max(2, (r.spend / top) * ih) : 0
          const hP = r.payments ? Math.max(2, (r.payments / top) * ih) : 0
          return (
            <g
              key={r.month}
              className="bar"
              {...bind(
                <>
                  <div style={{ marginBottom: 4 }}>{r.month}</div>
                  <div><i className="swatch" style={{ background: 'var(--s1)' }} /> {leftLabel} <b>{money2(r.spend)}</b></div>
                  <div><i className="swatch" style={{ background: 'var(--s2)' }} /> {rightLabel} <b>{money2(r.payments)}</b></div>
                </>,
              )}
            >
              {/* 2px gap between adjacent fills keeps the pair readable */}
              <rect x={cx - bw - 1} y={padT + ih - hS} width={bw} height={hS} rx={4} fill="var(--s1)" />
              <rect x={cx + 1} y={padT + ih - hP} width={bw} height={hP} rx={4} fill="var(--s2)" />
              <text className="ax" x={cx} y={H - 14} textAnchor="middle">{fmtMonth(r.month, i)}</text>
            </g>
          )
        })}
        <line x1={padL} x2={W - padR} y1={padT + ih} y2={padT + ih} stroke="var(--axis)" />
      </svg>
      <Tooltip tip={tip} />
    </>
  )
}
