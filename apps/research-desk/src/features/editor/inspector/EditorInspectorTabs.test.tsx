import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'

import { TooltipProvider } from '@/components/ui/tooltip'
import { LocaleProvider } from '@/i18n/LocaleProvider'
import { EditorInspector } from './EditorInspector'

describe('EditorInspector tab labels', () => {
  it('keeps long labels bounded while preserving their accessible names and counters', () => {
    const markup = renderToStaticMarkup(
      <LocaleProvider initialLocale="de">
        <TooltipProvider>
          <EditorInspector
            activeTab="assistant"
            assistant={<div />}
            canDecide
            changes={[{
              author: { color: '#2563eb', id: 'user-1', name: 'Owner' },
              createdAt: 1,
              id: 'change-1',
              originalText: 'before',
              position: 0,
              proposedText: 'after',
              suggestionIds: ['suggestion-1'],
              type: 'replacement',
            }]}
            changesError={null}
            changesView="open"
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
            commentCount={3}
            comments={<div />}
            commentUnreadCount={2}
            decisionError={null}
            display="simple"
            history={[]}
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

    expect(markup).toContain('aria-label="Kommentare 2"')
    expect(markup).toContain('aria-label="Änderungen 1"')
    expect(markup).toContain('aria-label="KI"')
    expect(markup).toContain('min-w-0 truncate">Kommentare</span>')
    expect(markup).toContain('min-w-0 truncate">Änderungen</span>')
    expect(markup).toContain('t-hint shrink-0 tabular-nums')
  })
})
