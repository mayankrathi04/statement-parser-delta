import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import type { PortalUser } from '../api'

/** Two initials from a display name — "Ada Lovelace" → "AL", "ada" → "AD". */
function initials(name: string): string {
  const parts = name.trim().split(/\s+/).filter(Boolean)
  if (!parts.length) return '?'
  const letters = parts.length === 1 ? parts[0].slice(0, 2) : parts[0][0] + parts[parts.length - 1][0]
  return letters.toUpperCase()
}

export default function ProfileMenu({
  user, theme, onToggleTheme, onSignOut,
}: {
  user: PortalUser
  theme: 'light' | 'dark' | null
  onToggleTheme: () => void
  onSignOut: () => void
}) {
  const [open, setOpen] = useState(false)
  const box = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const away = (event: MouseEvent) => {
      if (box.current && !box.current.contains(event.target as Node)) setOpen(false)
    }
    const escape = (event: KeyboardEvent) => { if (event.key === 'Escape') setOpen(false) }
    document.addEventListener('mousedown', away)
    document.addEventListener('keydown', escape)
    return () => {
      document.removeEventListener('mousedown', away)
      document.removeEventListener('keydown', escape)
    }
  }, [])

  return (
    <div className="profile" ref={box}>
      <button
        className="avatar"
        aria-haspopup="menu"
        aria-expanded={open}
        title={user.display_name}
        onClick={() => setOpen((value) => !value)}
      >
        {initials(user.display_name || user.username)}
      </button>
      {open && (
        <div className="profile-panel" role="menu">
          <div className="profile-head">
            <span className="avatar lg" aria-hidden="true">
              {initials(user.display_name || user.username)}
            </span>
            <span className="profile-id">
              <b>{user.display_name}</b>
              <span className="sub">@{user.username}</span>
            </span>
          </div>
          <div className="profile-items">
            <Link className="profile-item" to="/members" role="menuitem" onClick={() => setOpen(false)}>
              <span>Members</span>
              <span className="sub">{user.members.length}</span>
            </Link>
            <button className="profile-item" role="menuitem" onClick={onToggleTheme}>
              <span>Appearance</span>
              <span className="sub">{theme === 'dark' ? 'Dark' : 'Light'}</span>
            </button>
            <button className="profile-item danger" role="menuitem" onClick={onSignOut}>
              Sign out
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
