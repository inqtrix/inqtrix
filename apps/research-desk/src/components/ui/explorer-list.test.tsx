import { TooltipProvider } from '@radix-ui/react-tooltip'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import { readFileSync } from 'node:fs'

import { EXPLORER_SECTION_REVEAL_STEP, ExplorerHistoryRow } from './explorer-list'

function renderRow(labels: string[]): string {
  return renderToStaticMarkup(
    <TooltipProvider>
      <ExplorerHistoryRow
        actions={labels.map((label) => ({ icon: <span />, label, onSelect: () => undefined }))}
        onSelect={() => undefined}
        timeLabel="1 Tag"
        title={<span>Untitled.md</span>}
      />
    </TooltipProvider>,
  )
}

/** The offset a row's action button carries, keyed by its aria-label. */
function offsetsByLabel(markup: string): Record<string, string | null> {
  const found: Record<string, string | null> = {}
  for (const button of markup.match(/<button[^>]*>/g) ?? []) {
    const label = button.match(/aria-label="([^"]*)"/)?.[1]
    if (!label) continue
    found[label] = button.match(/right-(?:1|7|13)\b/)?.[0] ?? null
  }
  return found
}

describe('ExplorerHistoryRow trailing actions', () => {
  it('lays actions out from the right edge inward', () => {
    expect(offsetsByLabel(renderRow(['Löschen']))).toMatchObject({ 'Löschen': 'right-1' })
    expect(offsetsByLabel(renderRow(['Anheften', 'Löschen']))).toMatchObject({
      'Anheften': 'right-7',
      'Löschen': 'right-1',
    })
    expect(offsetsByLabel(renderRow(['Details', 'Anheften', 'Löschen']))).toMatchObject({
      'Details': 'right-13',
      'Anheften': 'right-7',
      'Löschen': 'right-1',
    })
  })

  it('never renders an action without an offset', () => {
    // An action beyond the available slots would lose its `right-*` class and
    // fall to the row's leading edge, on top of the type icon.
    for (const labels of [['a'], ['a', 'b'], ['a', 'b', 'c']]) {
      const offsets = Object.values(offsetsByLabel(renderRow(labels)))
      expect(offsets).toHaveLength(labels.length)
      expect(offsets.every((offset) => offset !== null)).toBe(true)
    }
  })
})

describe('reveal controls belong to folders only', () => {
  // A reveal control belongs to a folder section only. An ungrouped list
  // already scrolls, so a cap there hides entries the user could simply have
  // scrolled to. A folder is the one place a cap earns its keep, because a
  // folder can hold hundreds of entries.
  //
  // These read the SOURCE rather than a render: the section components are
  // internal to their panels and take 60+ props, so a render fixture would
  // cost more than it protects and would itself drift. Bound stated: this
  // catches a reveal control returning to a pseudo-folder, not every way
  // one could be reintroduced.
  const editor = readFileSync(
    'src/features/editor/EditorWorkspace.tsx',
    'utf8',
  )

  it('keeps the step at 8', () => {
    expect(EXPLORER_SECTION_REVEAL_STEP).toBe(8)
  })

  it.each(['__shared__', '__ungrouped__'])(
    'gives %s no reveal control — it is a list, not a folder',
    (sectionId) => {
      expect(editor).not.toContain(`showMoreDocuments('${sectionId}'`)
      expect(editor).not.toContain(`showLessDocuments('${sectionId}'`)
      expect(editor).not.toContain(`visibleDocumentCount('${sectionId}')`)
    },
  )

  it.each([
    ['chat', 'src/features/chat/history/ChatHistoryPanel.tsx'],
    ['knowledge', 'src/features/knowledge/KnowledgeHistoryPanel.tsx'],
  ])('renders the %s control only for a group section', (_name, file) => {
    const source = readFileSync(file, 'utf8')
    // Every usage sits behind the group flag.
    const usages = source.split('<ExplorerRevealControls').length - 1
    const guarded = source.split('{isGroupSection && (').length - 1
    expect(usages).toBe(1)
    expect(guarded).toBe(1)
  })
})
