import { useEffect, useRef, useState } from 'react'
import type { Member } from '../api'

export default function MemberSelect({
  members, selected, onChange,
}: {
  members: Member[]
  selected: Set<number>
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

  const label = selected.size === members.length
    ? `All members (${members.length})`
    : selected.size === 1
      ? members.find((member) => selected.has(member.id))?.name ?? '1 member'
      : `${selected.size} of ${members.length} members`

  return (
    <div className="ms" ref={box}>
      <div className="ms-btn" role="button" tabIndex={0} aria-expanded={open}
        onClick={() => setOpen((value) => !value)}>
        <span>{label}</span>
      </div>
      {open && <div className="ms-panel" role="listbox">
        {members.map((member) => <label className="ms-row" key={member.id}>
          <input type="checkbox" checked={selected.has(member.id)} onChange={() => {
            const next = new Set(selected)
            next.has(member.id) ? next.delete(member.id) : next.add(member.id)
            onChange(next.size ? next : new Set(members.map((item) => item.id)))
          }} />
          <span>{member.name}</span>
          {!!member.is_default && <span className="pill">default</span>}
        </label>)}
        <div className="ms-foot">
          <button className="link" onClick={() => onChange(new Set(members.map((m) => m.id)))}>
            Select all
          </button>
        </div>
      </div>}
    </div>
  )
}
