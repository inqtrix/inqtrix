import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { App } from '@/app/App'
import { AppProviders } from '@/app/AppProviders'
import { appFirstRenderLocale } from '@/app/guestLink'
import { RootErrorBoundary } from '@/app/RootErrorBoundary'
import 'katex/dist/katex.min.css'
import '@fontsource-variable/inter/index.css'
import '@/styles/globals.css'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <AppProviders initialLocale={appFirstRenderLocale(window.location.pathname)}>
      <RootErrorBoundary>
        <App />
      </RootErrorBoundary>
    </AppProviders>
  </StrictMode>,
)
