import { useCallback, useEffect, useState } from 'react'
import { Link, NavLink, Route, Routes, useLocation } from 'react-router-dom'
import { applyTheme, resolvedTheme, type Theme } from '@delta/theme/theme'
import { ApiError, api, type Bootstrap, type PortalUser } from './api'
import MemberSelect from './components/MemberSelect'
import ProfileMenu from './components/ProfileMenu'
import Analytics from './pages/Analytics'
import BankAccounts from './pages/BankAccounts'
import BankAnalysis from './pages/BankAnalysis'
import BankPipeline from './pages/BankPipeline'
import Cards from './pages/Cards'
import Categories from './pages/Categories'
import Connections from './pages/Connections'
import Members from './pages/Members'
import Pipeline from './pages/Pipeline'

/** Nine destinations in one row read as a wall. They divide cleanly into three
 *  subjects, so the top row names the subject and the row under it holds only
 *  that subject's pages. */
const GROUPS = [
  {
    key: 'cards',
    label: 'Cards',
    tabs: [
      { to: '/', label: 'Card Analysis', end: true },
      { to: '/cards', label: 'Cards' },
      { to: '/pipeline', label: 'Card Pipeline' },
    ],
  },
  {
    key: 'bank',
    label: 'Bank',
    tabs: [
      { to: '/bank', label: 'Bank Analysis', end: true },
      { to: '/bank-accounts', label: 'Bank Accounts' },
      { to: '/bank-pipeline', label: 'Bank Pipeline' },
    ],
  },
  {
    key: 'settings',
    label: 'Settings',
    tabs: [
      { to: '/categories', label: 'Categories' },
      { to: '/connections', label: 'Connections' },
      { to: '/members', label: 'Members' },
    ],
  },
] as const

/** Which subject the current URL belongs to, so a reload or a deep link opens
 *  with the right row of tabs already showing. */
function useActiveGroup() {
  const { pathname } = useLocation()
  return GROUPS.find((group) => group.tabs.some((tab) => (
    tab.to === '/' ? pathname === '/' : pathname.startsWith(tab.to)
  ))) ?? GROUPS[0]
}

/** The subject picker, which shares the first line with the app name. */
function GroupTabs() {
  const active = useActiveGroup()
  return (
    <nav className="nav nav-groups">
      {GROUPS.map((group) => (
        // The subject tab opens its first page: the row below it would
        // otherwise show tabs while the page still belongs to another subject.
        <Link key={group.key} to={group.tabs[0].to}
          className={group.key === active.key ? 'on' : ''}>
          {group.label}
        </Link>
      ))}
    </nav>
  )
}

/** Only the selected subject's pages, on the line under it. */
function SubTabs() {
  const active = useActiveGroup()
  return (
    <nav className="nav nav-sub">
      {active.tabs.map((tab) => (
        <NavLink key={tab.to} to={tab.to} end={'end' in tab ? tab.end : undefined}
          className={({ isActive }) => isActive ? 'on' : ''}>
          {tab.label}
        </NavLink>
      ))}
    </nav>
  )
}

function Auth({ onAuthenticated }: { onAuthenticated: (user: PortalUser) => void }) {
  const [registering, setRegistering] = useState(false)
  const [name, setName] = useState('')
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    api.authStatus().then((status) => setRegistering(status.registration_required)).catch(() => undefined)
  }, [])

  const submit = async () => {
    setBusy(true); setError(null)
    try {
      const result = registering
        ? await api.register(username, password, name)
        : await api.login(username, password)
      localStorage.setItem('sparser_token', result.token)
      onAuthenticated(result.user)
    } catch (caught) {
      setError(String((caught as Error).message))
    } finally {
      setBusy(false)
    }
  }

  return <div className="wrap" style={{ maxWidth: 520, paddingTop: 80 }}>
    <section className="card">
      <h1 style={{ marginTop: 0 }}>Statement Analyser</h1>
      <p className="hint">Private portal for your household’s card and bank analysis.</p>
      {registering && <input className="input" style={{ width: '100%', marginBottom: 10 }}
        placeholder="Your name" value={name} onChange={(event) => setName(event.target.value)} />}
      <input className="input" style={{ width: '100%', marginBottom: 10 }}
        placeholder="Username" value={username} onChange={(event) => setUsername(event.target.value)} />
      <input className="input" style={{ width: '100%', marginBottom: 10 }} type="password"
        placeholder="Password (8+ characters)" value={password}
        onChange={(event) => setPassword(event.target.value)}
        onKeyDown={(event) => event.key === 'Enter' && void submit()} />
      {error && <div className="banner" style={{ borderLeftColor: 'var(--critical)' }}>{error}</div>}
      <div style={{ display: 'flex', gap: 10, alignItems: 'center' }}>
        <button className="btn primary" disabled={busy || !username || password.length < 8 || (registering && !name)}
          onClick={() => void submit()}>{busy ? 'Please wait…' : registering ? 'Create private portal' : 'Sign in'}</button>
        <button className="link" onClick={() => { setRegistering((value) => !value); setError(null) }}>
          {registering ? 'Already have an account?' : 'Create an account'}
        </button>
      </div>
    </section>
  </div>
}

export default function App() {
  const [user, setUser] = useState<PortalUser | null>(null)
  const [checked, setChecked] = useState(false)
  const [boot, setBoot] = useState<Bootstrap | null>(null)
  // Seeded from what is actually on screen, OS included — starting at null made
  // the menu read "Light" on a dark-OS machine, and the first click of the
  // toggle then set 'dark' and appeared to do nothing.
  const [theme, setTheme] = useState<Theme>(() => resolvedTheme())
  /** Set when the session could not be checked because the API was unreachable. */
  const [offline, setOffline] = useState<string | null>(null)
  const [selectedMembers, setSelectedMembers] = useState<Set<number>>(new Set())

  const acceptUser = useCallback((next: PortalUser) => {
    setUser(next)
    setSelectedMembers(new Set(next.members.map((member) => member.id)))
    setChecked(true)
  }, [])

  const restore = useCallback(() => {
    const token = localStorage.getItem('sparser_token')
    if (!token) { setChecked(true); return }
    setOffline(null)
    api.me().then(acceptUser).catch((caught) => {
      // Only the server rejecting the token ends a session. A backend that is
      // restarting, or a dev proxy with nothing behind it, must not sign the user
      // out — the token is still valid and the session is still theirs.
      if (caught instanceof ApiError && caught.status === 401) {
        localStorage.removeItem('sparser_token')
      } else {
        setOffline(caught instanceof Error ? caught.message : String(caught))
      }
      setChecked(true)
    })
  }, [acceptUser])

  useEffect(() => {
    restore()
    const expired = () => { localStorage.removeItem('sparser_token'); setUser(null); setChecked(true) }
    window.addEventListener('sparser-auth-required', expired)
    return () => window.removeEventListener('sparser-auth-required', expired)
  }, [restore])

  const reload = useCallback(() => {
    if (!user) return
    api.bootstrap(selectedMembers).then(setBoot).catch(() => setBoot(null))
  }, [user, selectedMembers])
  useEffect(reload, [reload])
  useEffect(() => { applyTheme(theme) }, [theme])

  // The Members tab owns the roster, so a change there has to reach the top-bar
  // selector — and drop any member the selection still points at. Identity must be
  // stable: the Members page calls this from its load effect.
  const refreshMembers = useCallback(() => {
    api.members().then(({ members }) => {
      setUser((current) => (current ? { ...current, members } : current))
      setSelectedMembers((current) => {
        const live = new Set(members.map((member) => member.id))
        const kept = [...current].filter((id) => live.has(id))
        // Same members, same Set — a new one would re-run every member-scoped fetch.
        if (kept.length === current.size && kept.length) return current
        return new Set(kept.length ? kept : live)
      })
    }).catch(() => undefined)
  }, [])

  if (!checked) return <p className="empty">Opening private portal…</p>
  // Signed in, but the session could not be confirmed. Offer a retry rather than
  // a sign-in form: the credentials are fine, the server just was not there.
  if (!user && offline) {
    return <div className="wrap" style={{ maxWidth: 520, paddingTop: 80 }}>
      <section className="card">
        <h1 style={{ marginTop: 0 }}>Statement Analyser</h1>
        <div className="banner" style={{ borderLeftColor: 'var(--critical)' }}>{offline}</div>
        <p className="hint">
          You are still signed in — this only means the app could not reach the API just now.
        </p>
        <div style={{ display: 'flex', gap: 10, alignItems: 'center' }}>
          <button className="btn primary" onClick={restore}>Try again</button>
          <button className="link" onClick={() => {
            localStorage.removeItem('sparser_token'); setOffline(null)
          }}>Sign out instead</button>
        </div>
      </section>
    </div>
  }
  if (!user) return <Auth onAuthenticated={acceptUser} />

  const logout = () => {
    // Tell the server first so the token dies with the session, but never block
    // the sign-out on it: the local token goes either way.
    void api.logout().catch(() => undefined)
    localStorage.removeItem('sparser_token'); setUser(null); setBoot(null); setOffline(null)
  }

  return <>
    <div className="topbar"><div className="topbar-in">
      <div className="topbar-row">
        <span className="brand"><span className="brand-mark" aria-hidden="true">▤</span>Statement Analyser</span>
        <GroupTabs />
        <span className="spacer" />
        <MemberSelect members={user.members} selected={selectedMembers} onChange={setSelectedMembers} />
        <ProfileMenu
          user={user}
          theme={theme}
          onToggleTheme={() => setTheme((value) => (value === 'dark' ? 'light' : 'dark'))}
          onSignOut={logout}
        />
      </div>
      <SubTabs />
    </div></div>
    <div className="wrap"><Routes>
      <Route path="/" element={<Analytics boot={boot} members={selectedMembers} />} />
      <Route path="/cards" element={<Cards roster={user.members} members={selectedMembers} />} />
      <Route path="/pipeline" element={
        <Pipeline boot={boot} onChanged={reload} roster={user.members} members={selectedMembers} />
      } />
      <Route path="/bank" element={<BankAnalysis members={selectedMembers} />} />
      <Route path="/bank-accounts" element={
        <BankAccounts roster={user.members} members={selectedMembers} />
      } />
      <Route path="/bank-pipeline" element={
        <BankPipeline onChanged={reload} roster={user.members} members={selectedMembers} />
      } />
      <Route path="/connections" element={<Connections onChanged={reload} members={user.members} />} />
      <Route path="/categories" element={<Categories />} />
      <Route path="/members" element={<Members onChanged={refreshMembers} />} />
    </Routes></div>
  </>
}
