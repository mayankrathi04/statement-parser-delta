import { useEffect, useRef, useState } from 'react'
import type { Card } from '../api'

export default function CardSelect({
  cards,
  selected,
  colourOf,
  onChange,
  unrecognized,
}: {
  cards: Card[]
  selected: Set<number>
  colourOf: (id: number) => string
  onChange: (next: Set<number>) => void
  /**
   * Adds an "Unrecognized cards" row. A statement for a card that has never been
   * imported matches nothing in this list, so on a filtered scan it is silently
   * dropped — and it can never become a saved card, because only an import
   * creates one. Offering it here is the way out of that loop.
   */
  unrecognized?: { checked: boolean; onChange: (next: boolean) => void }
}) {
  const [open, setOpen] = useState(false)
  const box = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const away = (e: MouseEvent) => {
      if (box.current && !box.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', away)
    return () => document.removeEventListener('mousedown', away)
  }, [])

  const extra = unrecognized?.checked ? ' + unrecognized' : ''
  const label =
    (selected.size === 0 || selected.size === cards.length
      ? `All cards (${cards.length})`
      : selected.size === 1
        ? (cards.find((c) => selected.has(c.id))?.display_name ?? '1 card')
        : `${selected.size} of ${cards.length} cards`) + extra

  const toggle = (id: number) => {
    const next = new Set(selected)
    next.has(id) ? next.delete(id) : next.add(id)
    onChange(next)
  }

  return (
    <div className="ms" ref={box}>
      <div
        className="ms-btn"
        role="button"
        tabIndex={0}
        aria-expanded={open}
        aria-haspopup="listbox"
        onClick={() => setOpen((o) => !o)}
        onKeyDown={(e) => (e.key === 'Enter' || e.key === ' ') && setOpen((o) => !o)}
      >
        <span>{label}</span>
      </div>

      {open && (
        <div className="ms-panel" role="listbox">
          {cards.map((c) => (
            <label className="ms-row" key={c.id}>
              <input
                type="checkbox"
                checked={selected.has(c.id)}
                onChange={() => toggle(c.id)}
              />
              <i className="swatch" style={{ background: colourOf(c.id) }} />
              <span>{c.display_name}</span>
              <span style={{ marginLeft: 'auto', color: 'var(--text-muted)', fontSize: 12 }}>
                {c.txn_count}
              </span>
            </label>
          ))}
          {unrecognized && (
            <label
              className="ms-row"
              title="Statements whose card number matches none of the cards above — a card you have not imported yet"
              style={{ borderTop: '1px solid var(--border)' }}
            >
              <input
                type="checkbox"
                checked={unrecognized.checked}
                onChange={() => unrecognized.onChange(!unrecognized.checked)}
              />
              <i className="swatch" style={{ background: 'var(--text-muted)' }} />
              <span>Unrecognized cards</span>
            </label>
          )}
          <div className="ms-foot">
            <button className="link" onClick={() => onChange(new Set(cards.map((c) => c.id)))}>
              Select all
            </button>
            <button className="link" onClick={() => onChange(new Set())}>
              Clear
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
