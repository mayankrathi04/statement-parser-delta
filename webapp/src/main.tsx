import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import { initTheme } from '@delta/theme/theme'
import App from './App'
import './theme.css'

// Before the first render: stamps data-app (which selects this app's accent)
// and restores the stored light/dark choice. After the render it would be a
// visible snap from the OS theme to the stored one.
initTheme('statement-parser')

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </StrictMode>,
)
