import { useEffect, useRef, useState } from 'react'
import type { CategoryFacet } from '../api'

/** The category filter shared by Card Analysis and Bank Analysis.
 *
 *  Options come from the bootstrap facets rather than from the analytics
 *  response, so the list stays whole while the filter is applied — otherwise
 *  unticking a category would remove it from the menu that unticked it.
 */
export default function CategorySelect({
  categories,
  selected,
  onChange,
}: {
  categories: CategoryFacet[]
  selected: Set<string>
  onChange: (next: Set<string>) => void
}) {
  const [open, setOpen] = useState(false)
  const box = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const away = (event: MouseEvent) => {
      if (box.current && !box.current.contains(event.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', away)
    return () => document.removeEventListener('mousedown', away)
  }, [])

  const all = selected.size === categories.length && categories.length > 0
  const none = selected.size === 0
  const label = all
    ? `All categories (${categories.length})`
    : none
      ? 'No categories'
      : selected.size === 1
        ? [...selected][0]
        : `${selected.size} of ${categories.length} categories`

  const toggle = (name: string) => {
    const next = new Set(selected)
    next.has(name) ? next.delete(name) : next.add(name)
    onChange(next)
  }

  // One control both ways round: ticked when everything is on, and clicking it
  // turns everything off again.
  const toggleAll = () => onChange(all ? new Set() : new Set(categories.map((c) => c.name)))

  return (
    <div className="ms" ref={box}>
      <div
        className="ms-btn"
        role="button"
        tabIndex={0}
        aria-expanded={open}
        aria-haspopup="listbox"
        onClick={() => setOpen((value) => !value)}
        onKeyDown={(e) => (e.key === 'Enter' || e.key === ' ') && setOpen((value) => !value)}
      >
        <span>{label}</span>
      </div>

      {open && (
        <div className="ms-panel" role="listbox">
          {categories.length === 0 && <div className="ms-row">No categories yet</div>}
          {categories.length > 0 && (
            <label className="ms-row">
              <input
                type="checkbox"
                checked={all}
                ref={(el) => { if (el) el.indeterminate = !all && !none }}
                onChange={toggleAll}
              />
              <b>Select all</b>
              <span style={{ marginLeft: 'auto', color: 'var(--text-muted)', fontSize: 12 }}>
                {selected.size}/{categories.length}
              </span>
            </label>
          )}
          {categories.map((category) => (
            <label className="ms-row" key={category.name}>
              <input
                type="checkbox"
                checked={selected.has(category.name)}
                onChange={() => toggle(category.name)}
              />
              <span>{category.name}</span>
              <span style={{ marginLeft: 'auto', color: 'var(--text-muted)', fontSize: 12 }}>
                {category.n}
              </span>
            </label>
          ))}
        </div>
      )}
    </div>
  )
}
