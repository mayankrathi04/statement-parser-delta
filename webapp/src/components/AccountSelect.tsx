import { useEffect, useRef, useState } from 'react'
import type { BankAccount } from '../api'

export default function AccountSelect({
  accounts,
  selected,
  colourOf,
  onChange,
  unrecognized,
}: {
  accounts: BankAccount[]
  selected: Set<number>
  colourOf: (id: number) => string
  onChange: (next: Set<number>) => void
  /**
   * Adds an "Unrecognized accounts" row. A statement for an account that has
   * never been imported matches nothing in this list, so on a filtered scan it
   * is silently dropped — and it can never become a saved account, because only
   * an import creates one. Offering it here is the way out of that loop.
   */
  unrecognized?: { checked: boolean; onChange: (next: boolean) => void }
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

  const extra = unrecognized?.checked ? ' + unrecognized' : ''
  const label =
    (selected.size === 0 || selected.size === accounts.length
      ? `All accounts (${accounts.length})`
      : selected.size === 1
        ? (accounts.find((account) => selected.has(account.id))?.display_name ?? '1 account')
        : `${selected.size} of ${accounts.length} accounts`) + extra

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
              <span style={{ marginLeft: 'auto', color: 'var(--text-muted)', fontSize: 12 }}>
                {account.txn_count}
              </span>
            </label>
          ))}
          {unrecognized && (
            <label
              className="ms-row"
              title="Statements whose account number matches none of the accounts above — an account you have not imported yet"
              style={{ borderTop: '1px solid var(--border)' }}
            >
              <input
                type="checkbox"
                checked={unrecognized.checked}
                onChange={() => unrecognized.onChange(!unrecognized.checked)}
              />
              <i className="swatch" style={{ background: 'var(--text-muted)' }} />
              <span>Unrecognized accounts</span>
            </label>
          )}
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
