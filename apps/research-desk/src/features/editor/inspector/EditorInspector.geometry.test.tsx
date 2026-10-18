import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import { TooltipProvider } from '@/components/ui/tooltip'
import { LocaleProvider } from '@/i18n/LocaleProvider'
import { EditorCollaborationStatus, EditorWriteModeControl } from './EditorInspector'
import { buildEditorCollaborationStatusModel } from './model'
import type { EditorWriteMode } from './model'

// Geometry contracts for the editor top bar. The regression class these pin:
// the header is a 4-column grid whose middle columns are content-sized, so
// ANY width change there (a lock glyph appearing, an avatar joining, a label
// swapping) shifted the toolbar, the title truncation point and the actions
// in one visible jolt while the collaboration session came up. State changes
// may recolor; they must not re-measure.

function writeModeMarkup(state: 'locked' | 'unlocked'): string {
  return renderToStaticMarkup(
    <LocaleProvider initialLocale="de">
      <TooltipProvider>
        <EditorWriteModeControl
          access={state === 'locked' ? null : 'edit'}
          canEdit={state === 'unlocked'}
          collaborationActive
          mode={(state === 'locked' ? 'view' : 'edit') as EditorWriteMode}
          onModeChange={() => undefined}
          sourceReadOnly={false}
        />
      </TooltipProvider>
    </LocaleProvider>,
  )
}

function statusMarkup(options: {
  collaborationExpected: boolean
  participants?: { color: string; id: string; name: string }[]
  variant: 'inspector' | 'topbar'
}): string {
  const active = options.collaborationExpected
  const model = buildEditorCollaborationStatusModel({
    access: active ? 'edit' : null,
    active,
    canEdit: active,
    connectionStatus: active ? 'connected' : 'inactive',
    durabilityStatus: 'idle',
    participants: options.participants ?? [],
    synced: active,
  })
  return renderToStaticMarkup(
    <LocaleProvider initialLocale="de">
      <EditorCollaborationStatus
        collaborationExpected={options.collaborationExpected}
        model={model}
        variant={options.variant}
      />
    </LocaleProvider>,
  )
}

describe('EditorWriteModeControl geometry', () => {
  it('renders the same number of glyphs locked and unlocked', () => {
    // Locked buttons once grew a LockKeyhole each (~72px across the group).
    const locked = writeModeMarkup('locked')
    const unlocked = writeModeMarkup('unlocked')
    const svgCount = (markup: string) => (markup.match(/<svg/g) ?? []).length
    expect(svgCount(locked)).toBe(svgCount(unlocked))
  })

  it('reserves the read-only badge slot in every mode', () => {
    // Outside view mode the badge is invisible, never absent — leaving and
    // entering view mode must not re-lay the group out.
    const locked = writeModeMarkup('locked')
    const unlocked = writeModeMarkup('unlocked')
    expect(locked).toContain('size-6')
    expect(unlocked).toContain('size-6')
    expect(unlocked).toContain('invisible')
    expect(locked).not.toContain('invisible')
  })
})

describe('EditorCollaborationStatus geometry', () => {
  it('gives the label a fixed track in BOTH variants', () => {
    // "Wird synchronisiert" -> "Gespeichert" swaps very different widths;
    // only the topbar variant had a track, the inspector chip re-measured.
    const tracks = (['inspector', 'topbar'] as const).map((variant) => (
      statusMarkup({ collaborationExpected: true, variant })
        .match(/<span class="[^"]*\b(w-\[[\d.]+rem\])[^"]*" data-editor-status-label/)?.[1]
    ))
    expect(tracks[0]).toMatch(/^w-\[[\d.]+rem\]$/)
    expect(tracks[1]).toBe(tracks[0])
  })

  it('reserves the presence slot before anyone joins', () => {
    const alone = statusMarkup({ collaborationExpected: true, variant: 'topbar' })
    const together = statusMarkup({
      collaborationExpected: true,
      participants: [
        { color: '#2563EB', id: 'u1', name: 'Ada' },
        { color: '#DC2626', id: 'u2', name: 'Grace' },
      ],
      variant: 'topbar',
    })
    expect(alone).toContain('w-16')
    expect(together).toContain('w-16')
    expect(statusMarkup({ collaborationExpected: true, variant: 'inspector' }))
      .toContain('w-20')
  })

  it('shows a local document as Lokal from the first frame', () => {
    // Without the collaborationExpected gate the startup grace presented
    // 1.2s of "Wird synchronisiert" for a document that will never sync.
    const markup = statusMarkup({ collaborationExpected: false, variant: 'inspector' })
    expect(markup).toContain('Lokal')
    expect(markup).not.toContain('Wird synchronisiert')
  })
})
