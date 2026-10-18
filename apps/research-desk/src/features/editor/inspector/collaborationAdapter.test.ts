import { readFileSync } from 'node:fs'

import type { SuggestionDescriptor } from '@inqtrix/editor-schema'
import type { HocuspocusProvider } from '@hocuspocus/provider'
import type { Editor } from '@tiptap/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { EditorCollaborationActivity } from '@/api/inqtrixClient'
import { setRequestErrorLocale } from '@/api/requestErrorText'
import {
  bindInspectorSuggestionEvents,
  createActivityRefreshScheduler,
  durableSuggestionAuthorsFromActivity,
  editRefusalFromEvent,
  INSPECTOR_ACTIVITY_MIN_SPACING_MS,
  INSPECTOR_ATTRIBUTION_MAX_PAGES,
  inspectorActivityView,
  inspectorAttributionWarning,
  loadInspectorCollaborationActivity,
  normalizeActivity,
  readInspectorParticipants,
} from './collaborationAdapter'
import { buildInspectorChanges } from './model'
import {
  beginCollaborationAuthorityGuard,
  type CollaborationLiveAuthority,
} from '../collaborationAuthority'

const activity: EditorCollaborationActivity[] = [
  {
    actor: { id: 'reviewer', name: 'Grace Reviewer' },
    actor_kind: 'human',
    command_id: 'decision-command',
    created_at: 12,
    from_sequence: 4,
    suggestion_ids: ['suggestion-offline'],
    to_sequence: 5,
    type: 'decision',
  },
  {
    actor: { id: 'offline-author', name: 'Ada Offline' },
    actor_kind: 'human',
    command_id: 'suggestion-command',
    created_at: 10,
    from_sequence: 2,
    suggestion_ids: ['suggestion-offline'],
    to_sequence: 3,
    type: 'suggestion',
  },
]

function readableAuthority(
  overrides: Partial<CollaborationLiveAuthority> = {},
): CollaborationLiveAuthority {
  return {
    access: 'edit',
    blockingFailure: null,
    canEdit: true,
    connectionStatus: 'connected',
    documentId: 'document-1',
    generation: 2,
    lifecycleStatus: 'saved',
    revision: 4,
    synced: true,
    ...overrides,
  }
}

describe('durable collaboration attribution', () => {
  it('keeps canonical attribution when the remote author leaves before activity refresh', () => {
    const descriptors: SuggestionDescriptor[] = [{
      authorId: 'offline-author',
      createdAt: 10,
      kind: 'insertion',
      patchId: 'patch-offline',
      suggestionId: 'suggestion-offline',
    }]
    const excerpts = new Map([[
      'suggestion-offline',
      { deletionText: '', insertionText: 'Durable text', modificationText: '', position: 2 },
    ]])
    const awarenessStates = new Map<number, unknown>([[1, {
      user: { color: '#2563EB', id: 'offline-author', name: 'Ada Live' },
    }]])
    const provider = {
      awareness: { getStates: () => awarenessStates },
    } as unknown as HocuspocusProvider
    expect(readInspectorParticipants(provider, null).map((participant) => participant.name))
      .toEqual(['Ada Live'])

    awarenessStates.clear()
    const participantsAfterLeave = readInspectorParticipants(provider, null)
    const authors = durableSuggestionAuthorsFromActivity(activity)

    expect(participantsAfterLeave).toEqual([])
    expect(authors.get('suggestion-offline')?.name).toBe('Ada Offline')
    expect(buildInspectorChanges(descriptors, excerpts, participantsAfterLeave, authors)[0]?.author)
      .toMatchObject({ id: 'offline-author', name: 'Ada Offline' })
  })

  it('uses canonical activity actor names when every collaborator is offline', () => {
    expect(normalizeActivity(activity).map((entry) => entry.actor.name))
      .toEqual(['Grace Reviewer', 'Ada Offline'])
  })

  it('pages beyond the newest 100 rows to attribute an older still-open suggestion', async () => {
    const recentActivity: EditorCollaborationActivity[] = Array.from(
      { length: 100 },
      (_, index) => ({
        actor: { id: `reviewer-${index}`, name: `Reviewer ${index}` },
        actor_kind: 'human' as const,
        command_id: `command-${index}`,
        created_at: 200 - index,
        from_sequence: 200 - index,
        suggestion_ids: [`closed-${index}`],
        to_sequence: 201 - index,
        type: 'decision' as const,
      }),
    )
    const olderSuggestionEvent = activity[1]!
    const fetchActivity = vi.fn(async (_documentId: string, options: { cursor?: string }) => (
      options.cursor
        ? { data: [olderSuggestionEvent], next_cursor: null, object: 'list' as const }
        : { data: recentActivity, next_cursor: 'older-page', object: 'list' as const }
    )) as never

    const result = await loadInspectorCollaborationActivity({
      documentId: 'document-1',
      fetchActivity,
      openSuggestionIds: ['suggestion-offline'],
      workspaceId: 'workspace-1',
    })

    expect(result.data).toHaveLength(100)
    expect(result.attributionComplete).toBe(true)
    expect(result.attributionData).toEqual([olderSuggestionEvent])
    expect(durableSuggestionAuthorsFromActivity([
      ...result.data,
      ...result.attributionData,
    ]).get('suggestion-offline')?.name).toBe('Ada Offline')
    expect(fetchActivity).toHaveBeenCalledTimes(2)
    expect(fetchActivity).toHaveBeenLastCalledWith(
      'document-1',
      expect.objectContaining({ cursor: 'older-page', limit: 100 }),
    )
  })

  it('stops after page two and does not publish a result when authority is revoked during that await', async () => {
    let authority = readableAuthority()
    const authoritySource = { readAuthority: () => authority }
    const authorityGuard = beginCollaborationAuthorityGuard(
      authoritySource,
      { documentId: 'document-1', generation: 2 },
      'read',
    )
    const fetchActivity = vi.fn(async (_documentId: string, options: { cursor?: string }) => {
      if (!options.cursor) {
        return { data: [], next_cursor: 'page-2', object: 'list' as const }
      }
      authority = readableAuthority({
        access: null,
        canEdit: false,
        connectionStatus: 'access_revoked',
        lifecycleStatus: 'error',
        revision: 5,
        synced: false,
      })
      return { data: [], next_cursor: 'page-3', object: 'list' as const }
    }) as never
    const stateUpdate = vi.fn()

    await expect(loadInspectorCollaborationActivity({
      authorityGuard,
      documentId: 'document-1',
      fetchActivity,
      openSuggestionIds: ['suggestion-offline'],
      workspaceId: 'workspace-1',
    }).then(stateUpdate)).rejects.toThrow('revoked')

    expect(fetchActivity).toHaveBeenCalledTimes(2)
    expect(stateUpdate).not.toHaveBeenCalled()
  })

  it('bounds endless unique cursors and reports incomplete attribution without exposing an id', async () => {
    let page = 0
    const fetchActivity = vi.fn(async () => {
      page += 1
      return { data: [], next_cursor: `cursor-${page}`, object: 'list' as const }
    }) as never

    const result = await loadInspectorCollaborationActivity({
      documentId: 'document-1',
      fetchActivity,
      openSuggestionIds: ['suggestion-secret-user-id'],
      workspaceId: 'workspace-1',
    })
    const warning = inspectorAttributionWarning(result, 'en')

    expect(fetchActivity).toHaveBeenCalledTimes(INSPECTOR_ATTRIBUTION_MAX_PAGES)
    expect(result).toMatchObject({
      attributionComplete: false,
      lookupLimited: true,
      unresolvedSuggestionCount: 1,
    })
    expect(warning).toContain('bounded 500-row activity lookup')
    expect(warning).not.toContain('suggestion-secret-user-id')
  })

  it('maps the suggestions an edit removed with their authors, members and guests alike', () => {
    const removing: EditorCollaborationActivity = {
      ...activity[1]!,
      actor: { id: 'guest-editor', kind: 'guest', link_label: 'Review', name: 'Gina Gast' },
      actor_kind: 'guest',
      rejected_suggestion_count: 3,
      rejected_suggestions: [
        { author: { id: 'user-ada', name: 'Ada' }, patch_id: 'patch-1', suggestion_id: 'suggestion-1' },
        {
          author: { id: 'guest-author', kind: 'guest', link_label: 'Review', name: '' },
          patch_id: 'patch-2',
          suggestion_id: 'suggestion-2',
        },
      ],
      type: 'direct',
    }

    const [entry] = normalizeActivity([removing])

    expect(entry?.actor).toMatchObject({ id: 'guest-editor', name: 'Gina Gast' })
    expect(entry?.removedSuggestionCount).toBe(3)
    expect(entry?.removedSuggestions).toEqual([
      { author: expect.objectContaining({ id: 'user-ada', name: 'Ada' }), patchId: 'patch-1', suggestionId: 'suggestion-1' },
      { author: expect.objectContaining({ id: 'guest-author', name: 'Guest' }), patchId: 'patch-2', suggestionId: 'suggestion-2' },
    ])
  })

  it('never exposes a raw actor id as the visible fallback name', () => {
    const unnamed: EditorCollaborationActivity = {
      ...activity[1]!,
      actor: { id: 'user-opaque-7f3a', name: '   ' },
    }

    expect(normalizeActivity([unnamed])[0]?.actor).toMatchObject({
      id: 'user-opaque-7f3a',
      name: 'Collaborator',
    })
  })

  it("names an erased account's placeholder in the reader's language", () => {
    const erased: EditorCollaborationActivity = {
      ...activity[1]!,
      actor: { id: 'user-placeholder', kind: 'deleted', name: 'Deleted account' },
    }

    setRequestErrorLocale('de')
    try {
      expect(normalizeActivity([erased])[0]?.actor).toMatchObject({
        id: 'user-placeholder',
        name: 'Gelöschtes Konto',
      })
    } finally {
      setRequestErrorLocale('en')
    }
  })
})

describe('inspector suggestion event lifecycle', () => {
  it('removes listeners from the captured view after Tiptap destroys the editor', () => {
    const viewDom = new EventTarget()
    let destroyed = false
    let viewReads = 0
    const editor = {
      get isDestroyed() {
        return destroyed
      },
      get view() {
        viewReads += 1
        if (destroyed) throw new Error('editor view is not available')
        return { dom: viewDom }
      },
    } as unknown as Editor
    const onCollision = vi.fn()
    const onError = vi.fn()

    const unbind = bindInspectorSuggestionEvents(editor, { onCollision, onError })
    destroyed = true

    expect(() => unbind?.()).not.toThrow()
    expect(viewReads).toBe(1)
  })

  it('does not resolve the view of an editor that is already destroyed', () => {
    const view = vi.fn(() => {
      throw new Error('editor view is not available')
    })
    const editor = {
      isDestroyed: true,
      get view() {
        return view()
      },
    } as unknown as Editor

    expect(bindInspectorSuggestionEvents(editor, {
      onCollision: vi.fn(),
      onError: vi.fn(),
    })).toBeNull()
    expect(view).not.toHaveBeenCalled()
  })
})

describe('editor refusal events', () => {
  const event = (detail: unknown) => new CustomEvent('refusal', { detail })

  it('carries the reason and message of a refusal, and nothing for a cleared one', () => {
    expect(editRefusalFromEvent(event({
      message: 'In suggest mode a new block can only start at the end of a block.',
      reason: 'split_moves_content',
    }))).toEqual({
      message: 'In suggest mode a new block can only start at the end of a block.',
      reason: 'split_moves_content',
    })
    expect(editRefusalFromEvent(event(null))).toBeNull()
  })

  it('reports a reason it does not know as unknown and says so in the log', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined)
    try {
      expect(editRefusalFromEvent(event({ message: 'Changed rules.', reason: 'renamed_reason' })))
        .toEqual({ message: 'Changed rules.', reason: 'unknown' })
      expect(warn).toHaveBeenCalledOnce()
    } finally {
      warn.mockRestore()
    }
  })
})

describe('inspector activity refresh planning', () => {
  afterEach(() => {
    vi.useRealTimers()
  })

  function plannedReads() {
    vi.useFakeTimers()
    const reads: Array<{ finish: () => void; startedAt: number }> = []
    const scheduler = createActivityRefreshScheduler<ReturnType<typeof setTimeout>>({
      cancel: (handle) => clearTimeout(handle),
      minSpacingMs: INSPECTOR_ACTIVITY_MIN_SPACING_MS,
      now: () => Date.now(),
      run: () => new Promise<void>((resolve) => {
        reads.push({ finish: resolve, startedAt: Date.now() })
      }),
      schedule: (callback, delayMs) => setTimeout(callback, delayMs),
    })
    return { reads, scheduler }
  }

  it('reads three seconds apart at most, with one follow-up for any number of changes meanwhile', async () => {
    const { reads, scheduler } = plannedReads()
    const startedAt = Date.now()

    scheduler.request()
    expect(reads).toHaveLength(1)
    for (let index = 0; index < 100; index += 1) {
      scheduler.request()
      await vi.advanceTimersByTimeAsync(10)
    }
    expect(reads).toHaveLength(1)
    reads[0]?.finish()
    await vi.advanceTimersByTimeAsync(0)
    expect(reads).toHaveLength(1)

    await vi.advanceTimersByTimeAsync(startedAt + 3_000 - Date.now() - 1)
    expect(reads).toHaveLength(1)
    await vi.advanceTimersByTimeAsync(1)
    expect(reads).toHaveLength(2)
    expect(reads[1]!.startedAt - reads[0]!.startedAt).toBe(3_000)

    reads[1]?.finish()
    await vi.advanceTimersByTimeAsync(60_000)
    expect(reads).toHaveLength(2)
    scheduler.dispose()
  })

  it('reads at once when the previous read started three seconds ago or more', async () => {
    const { reads, scheduler } = plannedReads()

    scheduler.request()
    reads[0]?.finish()
    await vi.advanceTimersByTimeAsync(3_000)
    scheduler.request()

    expect(reads).toHaveLength(2)
    scheduler.dispose()
  })

  it('starts nothing after it is disposed while a read waits for its spacing', async () => {
    const { reads, scheduler } = plannedReads()

    scheduler.request()
    reads[0]?.finish()
    await vi.advanceTimersByTimeAsync(0)
    scheduler.request()
    scheduler.dispose()
    await vi.advanceTimersByTimeAsync(10_000)

    expect(reads).toHaveLength(1)
  })

  it('collects the changes made while it waits for the spacing into one read', async () => {
    const { reads, scheduler } = plannedReads()

    scheduler.request()
    reads[0]?.finish()
    await vi.advanceTimersByTimeAsync(0)
    for (let index = 0; index < 50; index += 1) {
      scheduler.request()
      await vi.advanceTimersByTimeAsync(20)
    }
    await vi.advanceTimersByTimeAsync(3_000)

    expect(reads).toHaveLength(2)
    expect(reads[1]!.startedAt - reads[0]!.startedAt).toBe(3_000)
    scheduler.dispose()
  })

  it('follows a read that is still running with exactly one more after it ends', async () => {
    const { reads, scheduler } = plannedReads()

    scheduler.request()
    scheduler.request()
    scheduler.request()
    await vi.advanceTimersByTimeAsync(5_000)
    expect(reads).toHaveLength(1)
    reads[0]?.finish()
    await vi.advanceTimersByTimeAsync(0)
    expect(reads).toHaveLength(2)
    reads[1]?.finish()
    await vi.advanceTimersByTimeAsync(10_000)

    expect(reads).toHaveLength(2)
    scheduler.dispose()
  })
})

describe('inspector activity read session', () => {
  // Bound stated: this reads the SOURCE, because the node tests render no
  // effects. The read session (guard, abort controller, scheduler) must
  // outlive document changes: a change asks the scheduler for a read and
  // never restarts the session, which would abort the read in flight.
  const source = readFileSync('src/features/editor/inspector/collaborationAdapter.ts', 'utf8')
  const sessionStart = source.indexOf('const scheduler = createActivityRefreshScheduler({')
  const dependencies = source.slice(
    source.indexOf('}, [', sessionStart),
    source.indexOf('])', sessionStart),
  )

  it('keeps one read session across new revisions and suggestion changes', () => {
    expect(source.split('const scheduler = createActivityRefreshScheduler({').length - 1).toBe(1)
    expect(dependencies).toContain('reading,')
    expect(dependencies).toContain('authorityRevision,')
    expect(dependencies).not.toContain('changeKey')
    expect(dependencies).not.toContain('activityRevision')
    expect(dependencies).not.toContain('openSuggestionIds')
  })

  it('reads only while the activity is shown on a visible page, and keeps the loaded lists while hidden', () => {
    // The open inspector's changes tab and the page's visibility decide
    // WHETHER to read; the session alone decides whether the loaded lists
    // still belong to it, so a hidden inspector keeps them stable instead of
    // answering with empty ones.
    const sessionEffect = source.slice(source.lastIndexOf('useEffect(() => {', sessionStart), sessionStart)
    const viewStart = source.indexOf('const view = inspectorActivityView(requestState, {')
    const viewKeys = source.slice(viewStart, source.indexOf('})', viewStart))

    expect(source.split('const reading = ').length - 1).toBe(1)
    expect(source).toContain('const reading = active && visible && pageVisible\n')
    expect(source).toContain(
      'useSyncExternalStore(subscribePageVisibility, pageIsVisible, () => true)',
    )
    expect(source).toContain("return document.visibilityState === 'visible'")
    expect(sessionEffect).toContain('if (!reading || !documentId || generation === null) return')
    expect(viewStart).toBeGreaterThan(-1)
    expect(viewKeys).toContain('\n    active,\n')
    expect(viewKeys).not.toContain('reading')
    expect(viewKeys).not.toContain('visible')
  })
})

describe('inspector activity view', () => {
  const loaded = {
    activity,
    attributionActivity: activity.slice(1),
    attributionWarning: null,
    authorityRevision: 3,
    documentId: 'document-1',
    error: null,
    isLoading: false,
    lifecycleKey: 'lifecycle-1',
  }
  const keys = {
    active: true,
    authorityRevision: 3,
    documentId: 'document-1',
    lifecycleKey: 'lifecycle-1',
  }

  it('keeps the loaded lists by identity while they belong to the session', () => {
    const view = inspectorActivityView(loaded, keys)

    expect(view.activity).toBe(loaded.activity)
    expect(view.attributionActivity).toBe(loaded.attributionActivity)
  })

  it('answers a request of another session with the same empty lists on every render', () => {
    const first = inspectorActivityView(loaded, { ...keys, authorityRevision: 4 })
    const second = inspectorActivityView(loaded, { ...keys, authorityRevision: 4 })

    expect(first.activity).toEqual([])
    expect(first.activity).toBe(second.activity)
    expect(first.attributionActivity).toBe(second.attributionActivity)
    expect(first.isLoading).toBe(false)
  })
})
