import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import { LocaleProvider } from '@/i18n/LocaleProvider'
import { EditorCollaborationStatus } from './EditorInspector'
import { EditorTopBarLayout } from './EditorTopBarLayout'
import { buildEditorCollaborationStatusModel } from './model'

const participants = [
  { color: '#2563EB', id: 'ada', name: 'Ada Lovelace' },
  { color: '#047857', id: 'lin', name: 'Lin Chen' },
  { color: '#B45309', id: 'max', name: 'Max Weber' },
  { color: '#7C3AED', id: 'zoe', name: 'Zoe Smith' },
]

function statusMarkup() {
  const model = buildEditorCollaborationStatusModel({
    access: 'edit',
    active: true,
    canEdit: true,
    connectionStatus: 'connected',
    durabilityStatus: 'saved',
    participants,
    projectionUpdatedAt: '2026-07-15T10:02:00.000Z',
    synced: true,
  })
  return renderToStaticMarkup(
    <LocaleProvider initialLocale="de">
      <EditorCollaborationStatus collaborationExpected model={model} variant="topbar" />
    </LocaleProvider>,
  )
}

describe('EditorCollaborationStatus', () => {
  it('announces projection confirmation and every participant while previewing only three', () => {
    const markup = statusMarkup()

    expect(markup).toContain('role="status"')
    expect(markup).toMatch(
      /<div(?=[^>]*role="status")(?=[^>]*data-editor-status-kind="saved")[^>]*>/,
    )
    expect(markup).not.toMatch(/<span[^>]*data-editor-status-kind=/)
    expect(markup).toContain('Bestätigter Stand')
    expect(markup).toContain('4 Teilnehmende: Ada Lovelace, Lin Chen, Max Weber, Zoe Smith')
    expect(markup).toContain('data-participant-count="4"')
    expect(markup.match(/data-participant-id=/g)).toHaveLength(3)
    expect(markup).toContain('+1')
  })

  it('keeps a long title and four-person status in bounded narrow topbar tracks', () => {
    const longTitle = 'A very long collaboration document title that must stay inside its own track'
    const markup = renderToStaticMarkup(
      <EditorTopBarLayout
        actions={(
          <LocaleProvider initialLocale="de">
            <EditorCollaborationStatus
              collaborationExpected
              model={buildEditorCollaborationStatusModel({
                access: 'edit',
                active: true,
                canEdit: true,
                connectionStatus: 'connected',
                durabilityStatus: 'saved',
                participants,
                synced: true,
              })}
              variant="topbar"
            />
          </LocaleProvider>
        )}
        leading={<span className="min-w-0 truncate">{longTitle}</span>}
        primary={<span>Mode and status</span>}
        toolbar={<span>Toolbar</span>}
      />,
    )

    expect(markup).toContain('data-editor-topbar="true"')
    expect(markup).toContain('data-editor-topbar-leading="true"')
    expect(markup).toContain('data-editor-topbar-toolbar="true"')
    expect(markup).toContain('data-editor-topbar-primary="true"')
    expect(markup).toContain('data-editor-topbar-actions="true"')
    expect(markup).toContain('overflow-hidden')
    expect(markup).not.toContain('overflow-x-auto')
    expect(markup).toContain('Mode and status')
    expect(markup).toContain(`>${longTitle}</span>`)
    expect(markup).toContain('data-participant-count="4"')
  })

  it('does not duplicate punctuation between a notice and projection metadata', () => {
    const markup = renderToStaticMarkup(
      <LocaleProvider initialLocale="de">
        <EditorCollaborationStatus
          collaborationExpected
          model={buildEditorCollaborationStatusModel({
            access: 'view',
            active: true,
            canEdit: false,
            connectionStatus: 'access_revoked',
            durabilityStatus: 'saved',
            notice: 'Der Zugriff wurde entzogen.',
            participants: [],
            projectionUpdatedAt: '2026-07-15T10:02:00.000Z',
            synced: false,
          })}
          variant="topbar"
        />
      </LocaleProvider>,
    )

    expect(markup).toContain(
      'Zugriff entzogen. Der Zugriff wurde entzogen. Bestätigter Stand',
    )
    expect(markup).not.toContain('entzogen..')
  })

  // Measured width of the longest status label that must never be cut,
  // "Synchronisierungsfehler", in the bundled Inter Variable at 11px: 126.7px
  // in Chromium, Firefox and WebKit alike. A reword or a type-scale change
  // must re-measure it here.
  const SYNC_ERROR_LABEL_WIDTH_PX = 126.7
  const REM_PX = 16

  function errorStatusMarkup(variant: 'inspector' | 'topbar') {
    return renderToStaticMarkup(
      <LocaleProvider initialLocale="de">
        <EditorCollaborationStatus
          collaborationExpected
          model={buildEditorCollaborationStatusModel({
            access: 'edit',
            active: true,
            canEdit: false,
            connectionStatus: 'error',
            durabilityStatus: 'error',
            participants,
            synced: false,
          })}
          variant={variant}
        />
      </LocaleProvider>,
    )
  }

  function remClass(markup: string, pattern: RegExp): number {
    const match = markup.match(pattern)
    if (!match?.[1]) throw new Error(`no class matches ${pattern}`)
    return Number.parseFloat(match[1]) * REM_PX
  }

  it.each(['inspector', 'topbar'] as const)('shows "Synchronisierungsfehler" uncut in the %s status', (variant) => {
    const markup = errorStatusMarkup(variant)
    const label = markup.match(/<span class="([^"]*)" data-editor-status-label="true">([^<]*)<\/span>/)
    expect(label?.[2]).toBe('Synchronisierungsfehler')
    const trackPx = remClass(label?.[1] ?? '', /\bw-\[([\d.]+)rem\]/)
    expect(trackPx).toBeGreaterThanOrEqual(SYNC_ERROR_LABEL_WIDTH_PX)
  })

  /** The max-width of the status container, from a spacing-scale class
   * (`max-w-56`, quarter rems) or an arbitrary rem value. */
  function statusMaxWidthPx(markup: string): number {
    const status = markup.match(/<div[^>]*class="([^"]*)"[^>]*role="status"/)
    const classes = status?.[1] ?? ''
    const arbitrary = classes.match(/\bmax-w-\[([\d.]+)rem\]/)
    if (arbitrary?.[1]) return Number.parseFloat(arbitrary[1]) * REM_PX
    const scale = classes.match(/\bmax-w-(\d+)\b/)
    if (scale?.[1]) return Number.parseInt(scale[1], 10) * REM_PX / 4
    throw new Error(`the status container has no max-width: ${classes}`)
  }

  it('leaves room for the whole label beside the presence slot inside the topbar chip', () => {
    const markup = errorStatusMarkup('topbar')
    const chipPx = statusMaxWidthPx(markup)
    const trackPx = remClass(markup, /\bw-\[([\d.]+)rem\][^"]*" data-editor-status-label/)
    // Button padding (2 x 6px), dot (8px), gap before the label (6px), gap
    // before the presence slot (8px) and the presence slot (w-16, 64px).
    expect(chipPx).toBeGreaterThanOrEqual(12 + 8 + 6 + trackPx + 8 + 64)
  })
})

