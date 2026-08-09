import { useEffect, useRef, useState } from 'react'
import type { Card } from '../api'

export default function CardSelect({
  cards,
  selected,
  colourOf,
  onChange,
}: {
  cards: Card[]
  selected: Set<number>
  colourOf: (id: number) => string
  onChange: (next: Set<number>) => void
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

  const label =
    selected.size === 0 || selected.size === cards.length
      ? `All cards (${cards.length})`
      : selected.size === 1
        ? (cards.find((c) => selected.has(c.id))?.display_name ?? '1 card')
        : `${selected.size} of ${cards.length} cards`

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
              <span style={{ marginLeft: 'auto', color: 'var(--muted)', fontSize: 12 }}>
                {c.txn_count}
              </span>
            </label>
          ))}
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
