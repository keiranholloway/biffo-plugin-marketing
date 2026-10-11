import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'

import App from './App'
import '@biffo/design-tokens/tokens.css'
import '@biffo/design-tokens/kit.css'
import './index.css'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
