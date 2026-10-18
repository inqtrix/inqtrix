import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'

import { LocaleProvider } from '@/i18n/LocaleProvider'
import { WebEvidenceSourceRow } from './WebEvidenceSourceRow'

describe('WebEvidenceSourceRow', () => {
  it('shows provider-grounded status and exposes a semantic inspection button', () => {
    const markup = renderToStaticMarkup(
      <LocaleProvider initialLocale="de">
        <WebEvidenceSourceRow
          onInspect={vi.fn()}
          reference={{
            chunkIndex: null,
            documentId: null,
            domain: 'example.test',
            excerpt: null,
            key: 'https://example.test/source',
            label: 'W1',
            pageNumber: null,
            sourceId: 'source-1',
            title: 'Official source',
            url: 'https://example.test/source',
          }}
        />
      </LocaleProvider>,
    )

    expect(markup).toContain('Websuche · provider-belegt')
    expect(markup).toContain('type="button"')
    expect(markup).toContain('aria-label="Websuchbeleg ansehen: Official source"')
    expect(markup).toContain('href="https://example.test/source"')
    expect(markup).toContain('rel="noreferrer noopener"')
  })

  it('renders a provider answer without inventing an external link', () => {
    const markup = renderToStaticMarkup(
      <LocaleProvider initialLocale="de">
        <WebEvidenceSourceRow
          onInspect={vi.fn()}
          reference={{
            chunkIndex: null,
            documentId: null,
            domain: null,
            excerpt: 'Frankreich gewann die WM 2018.',
            key: 'query:query-answer-only',
            label: 'W1',
            pageNumber: null,
            queryId: 'query-answer-only',
            sourceId: null,
            title: 'Fußball-Weltmeisterschaft 2018 Sieger',
            url: null,
          }}
        />
      </LocaleProvider>,
    )

    expect(markup).toContain('Websuche · provider-belegt')
    expect(markup).toContain(
      'aria-label="Websuchbeleg ansehen: Fußball-Weltmeisterschaft 2018 Sieger"',
    )
    expect(markup).not.toContain('href=')
  })
})
