import '@fontsource-variable/inter/index.css'
import 'katex/dist/katex.min.css'
import '@/styles/globals.css'

import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'

import { AppProviders } from '@/app/AppProviders'

import { GuestEditorPage } from '../GuestEditorPage'

// The real guest page with its real collaboration controller; the browser
// test answers every API request the page makes.
createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <AppProviders initialLocale="de">
      <GuestEditorPage token="egl1.fixture-token" />
    </AppProviders>
  </StrictMode>,
)
