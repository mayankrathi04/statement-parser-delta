import { useCallback, useEffect, useState } from 'react'
import { NavLink, Route, Routes } from 'react-router-dom'
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
      {error && <div className="banner" style={{ borderLeftColor: 'var(--crit)' }}>{error}</div>}
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
  const [theme, setTheme] = useState<'light' | 'dark' | null>(null)
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
  useEffect(() => { if (theme) document.documentElement.setAttribute('data-theme', theme) }, [theme])

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
        <div className="banner" style={{ borderLeftColor: 'var(--crit)' }}>{offline}</div>
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
      <span className="brand">Statement Analyser</span>
      <nav className="nav">
        <NavLink to="/" end className={({ isActive }) => isActive ? 'on' : ''}>Card Analysis</NavLink>
        <NavLink to="/cards" className={({ isActive }) => isActive ? 'on' : ''}>Cards</NavLink>
        <NavLink to="/pipeline" className={({ isActive }) => isActive ? 'on' : ''}>Card Pipeline</NavLink>
        <NavLink to="/bank" className={({ isActive }) => isActive ? 'on' : ''}>Bank Analysis</NavLink>
        <NavLink to="/bank-accounts" className={({ isActive }) => isActive ? 'on' : ''}>Bank Accounts</NavLink>
        <NavLink to="/bank-pipeline" className={({ isActive }) => isActive ? 'on' : ''}>Bank Pipeline</NavLink>
        <NavLink to="/categories" className={({ isActive }) => isActive ? 'on' : ''}>Categories</NavLink>
        <NavLink to="/connections" className={({ isActive }) => isActive ? 'on' : ''}>Connections</NavLink>
        <NavLink to="/members" className={({ isActive }) => isActive ? 'on' : ''}>Members</NavLink>
      </nav>
      <span className="spacer" />
      <MemberSelect members={user.members} selected={selectedMembers} onChange={setSelectedMembers} />
      <ProfileMenu
        user={user}
        theme={theme}
        onToggleTheme={() => setTheme((value) => value === 'dark' ? 'light' : 'dark')}
        onSignOut={logout}
      />
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
