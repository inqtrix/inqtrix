import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'

import { TooltipProvider } from '@/components/ui/tooltip'
import { LocaleProvider } from '@/i18n/LocaleProvider'
import { EditorInspector } from './EditorInspector'
import type { InspectorHistoryEntry, InspectorRemovedSuggestion } from './model'

const ADA = { color: '#2563eb', id: 'user-ada', name: 'Ada' }
const LIN = { color: '#047857', id: 'user-lin', name: 'Lin' }
const KIM = { color: '#b45309', id: 'user-kim', name: 'Kim' }

const BY_ADA: InspectorRemovedSuggestion = { author: ADA, patchId: 'patch-ada', suggestionId: 'suggestion-ada' }
const BY_KIM: InspectorRemovedSuggestion = { author: KIM, patchId: 'patch-kim', suggestionId: 'suggestion-kim' }
const BY_LIN: InspectorRemovedSuggestion = { author: LIN, patchId: 'patch-lin', suggestionId: 'suggestion-lin' }

/** Lin's direct edit that removed pending suggestions. */
function removingEdit(removed: InspectorRemovedSuggestion[], count = removed.length): InspectorHistoryEntry {
  return {
    actor: LIN,
    actorKind: 'human',
    commandId: null,
    createdAt: 1_784_112_000_000,
    fromSequence: 7,
    id: '7:7:direct',
    removedSuggestionCount: count,
    removedSuggestions: removed,
    suggestionIds: [],
    toSequence: 7,
    type: 'direct',
  }
}

function historyMarkup(
  entries: InspectorHistoryEntry[],
  locale: 'de' | 'en' = 'de',
): string {
  return renderToStaticMarkup(
    <LocaleProvider initialLocale={locale}>
      <TooltipProvider>
        <EditorInspector
          activeTab="changes"
          assistant={<div />}
          canDecide
          changes={[]}
          changesError={null}
          changesView="history"
          collaborationActive
          collaborationStatus={{
            active: true,
            hasUnconfirmedLocalChanges: false,
            kind: 'saved',
            nextReconnectAt: null,
            notice: null,
            participantOverflow: 0,
            participants: [],
            projectionConfirmedAt: null,
            reconnectAttempt: 0,
            recoverability: 'none',
            unconfirmedSince: null,
            visibleParticipants: [],
          }}
          commentCount={0}
          comments={<div />}
          commentUnreadCount={0}
          decisionError={null}
          display="simple"
          history={entries}
          historyError={null}
          historyFilters={{ actorId: null, type: null }}
          historyLoading={false}
          isDecisionPending={false}
          onActiveTabChange={vi.fn()}
          onChangesViewChange={vi.fn()}
          onClose={vi.fn()}
          onDecision={vi.fn()}
          onDisplayChange={vi.fn()}
          onHistoryFiltersChange={vi.fn()}
          onOpenFiltersChange={vi.fn()}
          onSelectedChangeIdChange={vi.fn()}
          openFilters={{ authorId: null, type: null }}
          selectedChangeId={null}
        />
      </TooltipProvider>
    </LocaleProvider>,
  )
}

// The headlines are the inspector's own prose; a reword must be caught here.
describe('the history entry of an edit that removed pending suggestions', () => {
  it('names who removed whose suggestion, the same for every reader', () => {
    const markup = historyMarkup([removingEdit([BY_ADA])])

    expect(markup).toContain('Lin hat einen Vorschlag von Ada entfernt')
    expect(markup).not.toContain('abgelehnt')
  })

  it('says withdrawn when the writer removed their own suggestion', () => {
    const markup = historyMarkup([removingEdit([BY_LIN])])

    expect(markup).toContain('Lin hat einen Vorschlag zurückgezogen')
    expect(markup).not.toContain('entfernt')
  })

  it('counts several removed suggestions', () => {
    expect(historyMarkup([removingEdit([BY_ADA, BY_KIM])])).toContain('Lin hat 2 Vorschläge entfernt')
    // More than the entry names: the count stays whole.
    expect(historyMarkup([removingEdit([BY_ADA, BY_KIM], 23)])).toContain('Lin hat 23 Vorschläge entfernt')
  })

  it('reads the same in English', () => {
    expect(historyMarkup([removingEdit([BY_ADA])], 'en')).toContain('Lin removed a suggestion by Ada')
    expect(historyMarkup([removingEdit([BY_LIN])], 'en')).toContain('Lin withdrew a suggestion')
    expect(historyMarkup([removingEdit([BY_ADA, BY_KIM])], 'en')).toContain('Lin removed 2 suggestions')
  })

  it('takes names literally', () => {
    const dollar = { ...ADA, name: 'A$&B' }
    const markup = historyMarkup([removingEdit([{ ...BY_ADA, author: dollar }])])

    expect(markup).toContain('Lin hat einen Vorschlag von A$&amp;B entfernt')
  })

  it('keeps the plain headline for an edit that removed nothing', () => {
    const markup = historyMarkup([removingEdit([], 0)])

    expect(markup).toContain('Lin bearbeitete das Dokument')
    expect(markup).not.toContain('entfernt')
  })

  it('names the kind of a German proposal as a capitalised noun', () => {
    const proposal: InspectorHistoryEntry = {
      ...removingEdit([], 0),
      actor: ADA,
      id: '6:6:suggestion',
      summary: [{ after: 'neu', before: '', kind: 'insertion', position: 3 }],
      suggestionIds: ['suggestion-ada'],
      type: 'suggestion',
    }

    expect(historyMarkup([proposal])).toContain('Ada schlug eine Einfügung vor')
  })
})
