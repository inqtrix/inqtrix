import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import { LocaleProvider } from '@/i18n/LocaleProvider'
import { LicensingPanel } from './SettingsWorkspace'

const LEGAL = {
  copyright: 'Copyright (c) 2026 Babak Zandi.',
  license: 'Apache-2.0',
  notice: 'Inqtrix - Copyright (c) 2026 Babak Zandi - https://github.com/inqtrix/inqtrix',
  project: 'Inqtrix',
  source_url: 'https://github.com/inqtrix/inqtrix',
  warranty_notice: 'This software is provided without warranties; see LICENSE for details.',
}

function panelMarkup(legal: typeof LEGAL | undefined): string {
  return renderToStaticMarkup(
    <LocaleProvider initialLocale="de">
      <LicensingPanel
        appVersion="0.2.0.9"
        legal={legal}
        projectSourceUrl={LEGAL.source_url}
      />
    </LocaleProvider>,
  )
}

describe('LicensingPanel', () => {
  it('opens with the project name, license tag and version', () => {
    const markup = panelMarkup(LEGAL)
    expect(markup).toContain('Inqtrix')
    expect(markup).toContain('Apache-2.0')
    expect(markup).toContain('v0.2.0.9')
  })

  it('does not repeat the copyright, project source, legal notice or warranty rows', () => {
    // The server still publishes these fields (/health.legal); the panel
    // shows license, version, usage notices and resources only.
    const markup = panelMarkup(LEGAL)
    expect(markup).not.toContain('Copyright (c)')
    // Row headings render as <h3>…</h3>; the usage notice may still mention
    // the warranty in a sentence, so the assertion targets the heading.
    expect(markup).not.toContain('>Projektquelle<')
    expect(markup).not.toContain('>Rechtlicher Hinweis<')
    expect(markup).not.toContain('>Gewährleistung<')
    expect(markup).not.toContain(LEGAL.notice)
    expect(markup).not.toContain(LEGAL.warranty_notice)
  })

  it('keeps the usage notices and the three resource links', () => {
    const markup = panelMarkup(LEGAL)
    expect(markup).toContain('Nutzungshinweise')
    expect(markup).toContain('Inqtrix ist ein KI-System.')
    for (const href of [
      'https://github.com/inqtrix/inqtrix',
      'https://github.com/inqtrix/inqtrix/blob/main/docs/README.md',
      'https://github.com/inqtrix/inqtrix/blob/main/LICENSE',
    ]) {
      expect(markup).toContain(`href="${href}"`)
    }
    expect(markup.match(/rel="noreferrer"/g)?.length).toBe(3)
  })

  it('falls back to the translated name and license without server legal data', () => {
    const markup = panelMarkup(undefined)
    expect(markup).toContain('Inqtrix')
    expect(markup).toContain('Apache-2.0')
    expect(markup).toContain('v0.2.0.9')
    expect(markup).not.toContain('Copyright (c)')
  })
})
