import { useEffect, useRef, useState } from 'react'
import type { BankAccount } from '../api'

export default function AccountSelect({
  accounts,
  selected,
  colourOf,
  onChange,
}: {
  accounts: BankAccount[]
  selected: Set<number>
  colourOf: (id: number) => string
  onChange: (next: Set<number>) => void
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

  const label =
    selected.size === 0 || selected.size === accounts.length
      ? `All accounts (${accounts.length})`
      : selected.size === 1
        ? (accounts.find((account) => selected.has(account.id))?.display_name ?? '1 account')
        : `${selected.size} of ${accounts.length} accounts`

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
        onClick={() => setOpen((value) => !value)}
        onKeyDown={(event) =>
          (event.key === 'Enter' || event.key === ' ') && setOpen((value) => !value)}
      >
        <span>{label}</span>
      </div>
      {open && (
        <div className="ms-panel" role="listbox">
          {accounts.map((account) => (
            <label className="ms-row" key={account.id}>
              <input
                type="checkbox"
                checked={selected.has(account.id)}
                onChange={() => toggle(account.id)}
              />
              <i className="swatch" style={{ background: colourOf(account.id) }} />
              <span>{account.display_name}</span>
              <span style={{ marginLeft: 'auto', color: 'var(--muted)', fontSize: 12 }}>
                {account.txn_count}
              </span>
            </label>
          ))}
          <div className="ms-foot">
            <button className="link" onClick={() => onChange(new Set(accounts.map((a) => a.id)))}>
              Select all
            </button>
            <button className="link" onClick={() => onChange(new Set())}>Clear</button>
          </div>
        </div>
      )}
    </div>
  )
}
