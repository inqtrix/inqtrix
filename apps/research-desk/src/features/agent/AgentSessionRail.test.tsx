import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'

import { TooltipProvider } from '@/components/ui/tooltip'
import { translations } from '@/i18n/translations'
import { AgentSessionRail } from './AgentSessionRail'
import type { AgentSessionRecord } from './model'

const localeState = vi.hoisted(() => ({ locale: 'de' as 'de' | 'en' }))

vi.mock('@/i18n/LocaleProvider', () => ({
  useLocale: () => ({ locale: localeState.locale, t: translations[localeState.locale] }),
}))

function rail(session: Partial<AgentSessionRecord>): string {
  const record = {
    createdAt: '2026-10-30T08:00:00.000Z',
    groupId: null,
    id: 'session-1',
    runIds: [],
    title: 'Plan Q3',
    updatedAt: '2026-10-30T08:00:00.000Z',
    ...session,
  } as AgentSessionRecord
  const noop = () => undefined
  return renderToStaticMarkup(
    <TooltipProvider>
      <AgentSessionRail
        onAdoptVisibleOrder={noop}
        onChangeSortMode={noop}
        onCreateSession={noop}
        onCreateSessionGroup={noop}
        onDeleteSession={noop}
        onDeleteSessionGroup={noop}
        onMoveSessionGroup={noop}
        onMoveSessionToGroup={noop}
        onRenameSession={noop}
        onRenameSessionGroup={noop}
        onRetrySessionDeletion={noop}
        onSelectSession={noop}
        onTogglePinnedSession={noop}
        pinnedSessionIds={[]}
        runs={{}}
        selectedSessionId={null}
        sessionGroupOrder={[]}
        sessionGroups={{}}
        sessionOrder={[record.id]}
        sessions={{ [record.id]: record }}
        sortMode="manual"
      />
    </TooltipProvider>,
  )
}

describe('the delete button of a session row', () => {
  it('names the session it deletes, like the chat, knowledge and editor rows', () => {
    // Every row otherwise reads the same "Sitzung löschen" to a screen reader.
    localeState.locale = 'de'
    expect(rail({})).toContain('aria-label="Sitzung löschen: Plan Q3"')
    localeState.locale = 'en'
    expect(rail({})).toContain('aria-label="Delete session: Plan Q3"')
    localeState.locale = 'de'
  })

  it('stays absent on a session that cannot be deleted', () => {
    expect(rail({ persistable: false })).not.toContain('Sitzung löschen')
  })
})
