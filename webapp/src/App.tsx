import { useCallback, useEffect, useState } from 'react'
import { NavLink, Route, Routes } from 'react-router-dom'
import { api, type Bootstrap } from './api'
import Analytics from './pages/Analytics'
import Cards from './pages/Cards'
import Connections from './pages/Connections'
import Pipeline from './pages/Pipeline'

export default function App() {
  const [boot, setBoot] = useState<Bootstrap | null>(null)
  const [theme, setTheme] = useState<'light' | 'dark' | null>(null)

  const reload = useCallback(() => {
    api.bootstrap().then(setBoot).catch(() => setBoot(null))
  }, [])

  useEffect(reload, [reload])

  useEffect(() => {
    if (theme) document.documentElement.setAttribute('data-theme', theme)
  }, [theme])

  return (
    <>
      <div className="topbar">
        <div className="topbar-in">
          <span className="brand">Card Spend Analyser</span>
          <nav className="nav">
            <NavLink to="/" end className={({ isActive }) => (isActive ? 'on' : '')}>
              Analytics
            </NavLink>
            <NavLink to="/cards" className={({ isActive }) => (isActive ? 'on' : '')}>
              Cards
            </NavLink>
            <NavLink to="/pipeline" className={({ isActive }) => (isActive ? 'on' : '')}>
              Pipeline
            </NavLink>
            <NavLink to="/connections" className={({ isActive }) => (isActive ? 'on' : '')}>
              Connections
            </NavLink>
          </nav>
          <span className="spacer" />
          <span className="sub">
            {boot
              ? `${boot.cards.length} cards · ${boot.statements.length} statements`
              : 'connecting…'}
          </span>
          <button
            className="btn"
            onClick={() => setTheme((t) => (t === 'dark' ? 'light' : 'dark'))}
            title="Toggle light and dark"
          >
            ◐
          </button>
        </div>
      </div>

      <div className="wrap">
        <Routes>
          <Route path="/" element={<Analytics boot={boot} />} />
          <Route path="/cards" element={<Cards />} />
          <Route path="/pipeline" element={<Pipeline boot={boot} onChanged={reload} />} />
          <Route path="/connections" element={<Connections onChanged={reload} />} />
        </Routes>
      </div>
    </>
  )
}
