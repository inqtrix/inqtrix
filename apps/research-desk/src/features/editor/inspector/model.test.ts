import type { SuggestionDescriptor } from '@inqtrix/editor-schema'
import { UNSUPPORTED_SUGGESTION_REASONS } from '@inqtrix/editor-schema'
import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  COLLABORATION_NOTICES,
  collaborationNoticeCode,
  type CollaborationNotice,
} from '../collaborationNotices'
import { COLLABORATION_EDIT_REFUSAL_REASONS } from '../tiptap'
import { collaborationEditorPolicyUpdate } from './editorPolicy'
import {
  COLLABORATION_EDIT_REFUSAL_COPY,
  GERMAN_COLLABORATION_NOTICES,
  localizeCollaborationEditRefusal,
  adjacentChangeId,
  beginCollaborationPublicationFocus,
  buildEditorCollaborationStatusModel,
  buildInspectorChanges,
  consumeCollaborationPublicationFocus,
  isCollaborationPublicationFocusCurrent,
  localizeEditorCollaborationNotice,
  pendingCollaborationPublicationFocusForDocument,
  registerCollaborationPublicationFocus,
  effectiveEditorWriteMode,
  filterInspectorChanges,
  editorDocumentRowKind,
  editorDocumentRailCapabilities,
  isOwnedEditorDocument,
  participantPreview,
  partitionEditorDocumentsByAccess,
  type InspectorChange,
  CalmCollaborationStatus,
  startupPresentation,
  type EditorCollaborationStatusKind,
} from './model'

const participants = [
  { color: '#111111', id: 'ada', name: 'Ada' },
  { color: '#222222', id: 'lin', name: 'Lin' },
  { color: '#333333', id: 'max', name: 'Max' },
  { color: '#444444', id: 'zoe', name: 'Zoe' },
]

const changes: InspectorChange[] = [
  {
    author: participants[0]!,
    createdAt: 10,
    id: 'patch-a',
    originalText: 'old',
    position: 2,
    proposedText: 'new',
    suggestionIds: ['suggestion-a'],
    type: 'replacement',
  },
  {
    author: participants[1]!,
    createdAt: 20,
    id: 'patch-b',
    originalText: '',
    position: 8,
    proposedText: 'addition',
    suggestionIds: ['suggestion-b'],
    type: 'insertion',
  },
]

describe('inspector collaboration model', () => {
  it('groups compound suggestions into one ordered patch row', () => {
    const descriptors: SuggestionDescriptor[] = [
      {
        authorId: 'ada',
        createdAt: 10,
        kind: 'deletion',
        patchId: 'patch-a',
        suggestionId: 'delete-a',
      },
      {
        authorId: 'ada',
        createdAt: 10,
        kind: 'insertion',
        patchId: 'patch-a',
        suggestionId: 'insert-a',
      },
    ]
    const excerpts = new Map([
      ['delete-a', { deletionText: 'before', insertionText: '', modificationText: '', position: 9 }],
      ['insert-a', { deletionText: '', insertionText: 'after', modificationText: '', position: 9 }],
    ])

    expect(buildInspectorChanges(descriptors, excerpts, participants)).toEqual([{
      author: participants[0],
      // A descriptor stamped in Unix seconds reaches the card in milliseconds.
      createdAt: 10_000,
      id: 'patch-a',
      originalText: 'before',
      position: 9,
      proposedText: 'after',
      suggestionIds: ['delete-a', 'insert-a'],
      type: 'replacement',
    }])
  })

  it('dates every change card in epoch milliseconds, whichever writer stamped the suggestion', () => {
    // Stored documents hold AI suggestions the collaboration service stamped
    // in Unix seconds before it moved to epoch milliseconds, the unit a
    // browser typing in suggest mode stamps. Both are read as they are stored.
    const publishedSeconds = 1_790_942_609
    const typedMilliseconds = 1_790_943_101_495
    const descriptors: SuggestionDescriptor[] = [
      {
        authorId: 'ada',
        createdAt: publishedSeconds,
        kind: 'insertion',
        patchId: 'patch-published',
        suggestionId: 'suggestion-published',
      },
      {
        authorId: 'lin',
        createdAt: typedMilliseconds,
        kind: 'insertion',
        patchId: 'patch-typed',
        suggestionId: 'suggestion-typed',
      },
    ]
    const excerpts = new Map([
      ['suggestion-published', { deletionText: '', insertionText: 'ai', modificationText: '', position: 4 }],
      ['suggestion-typed', { deletionText: '', insertionText: 'typed', modificationText: '', position: 4 }],
    ])

    const cards = buildInspectorChanges(descriptors, excerpts, participants)

    expect(cards.map((card) => [card.id, card.createdAt])).toEqual([
      ['patch-published', 1_790_942_609_000],
      ['patch-typed', 1_790_943_101_495],
    ])
    // The published card shows the day the suggestion was made.
    expect(new Date(cards[0]!.createdAt).toISOString()).toBe('2026-10-02T12:03:29.000Z')
  })

  it('keeps navigation and filters deterministic as remote rows change', () => {
    expect(filterInspectorChanges(changes, { authorId: 'lin', type: null }))
      .toEqual([changes[1]])
    expect(adjacentChangeId(changes, null, 1)).toBe('patch-a')
    expect(adjacentChangeId(changes, 'missing', -1)).toBe('patch-b')
    expect(adjacentChangeId(changes, 'patch-b', 1)).toBe('patch-b')
  })

  it('caps participant avatars at three and reports the overflow', () => {
    expect(participantPreview(participants)).toEqual({
      overflow: 1,
      visible: participants.slice(0, 3),
    })
  })

  it('derives one status model for lifecycle, durability, projection, and avatars', () => {
    const syncing = buildEditorCollaborationStatusModel({
      access: 'edit',
      active: true,
      canEdit: false,
      connectionStatus: 'connected',
      durabilityStatus: 'idle',
      participants,
      projectionUpdatedAt: '2026-07-15T10:00:00.000Z',
      synced: false,
    })
    expect(syncing).toMatchObject({
      projectionConfirmedAt: '2026-07-15T10:00:00.000Z',
      kind: 'syncing',
      participantOverflow: 1,
      visibleParticipants: participants.slice(0, 3),
    })

    const saving = buildEditorCollaborationStatusModel({
      access: 'edit',
      active: true,
      canEdit: true,
      connectionStatus: 'connected',
      durabilityStatus: 'pending',
      participants: [],
      projectionUpdatedAt: '2026-07-15T10:01:00.000Z',
      synced: true,
    })
    expect(saving).toMatchObject({
      kind: 'saving',
      projectionConfirmedAt: '2026-07-15T10:01:00.000Z',
    })
    // An acknowledgement that is overdue keeps the saving state, visibly
    // delayed; a failure still takes precedence.
    const delayedInput = {
      access: 'edit',
      active: true,
      canEdit: true,
      connectionStatus: 'connected',
      durabilityDelayed: true,
      durabilityStatus: 'pending',
      participants: [],
      synced: true,
    } as const
    expect(buildEditorCollaborationStatusModel(delayedInput).kind).toBe('saving_delayed')
    expect(buildEditorCollaborationStatusModel({
      ...delayedInput,
      durabilityStatus: 'error',
    }).kind).toBe('error')
    expect(buildEditorCollaborationStatusModel({
      ...delayedInput,
      durabilityDelayed: false,
    }).kind).toBe('saving')
    expect(buildEditorCollaborationStatusModel({
      access: 'edit',
      active: true,
      canEdit: true,
      connectionStatus: 'connected',
      durabilityStatus: 'saved',
      participants: [],
      projectionUpdatedAt: '2026-07-15T10:02:00.000Z',
      synced: true,
    })).toMatchObject({
      kind: 'saved',
      projectionConfirmedAt: '2026-07-15T10:02:00.000Z',
    })
    expect(buildEditorCollaborationStatusModel({
      access: 'view',
      active: true,
      canEdit: false,
      connectionStatus: 'read_only',
      durabilityStatus: 'saved',
      participants: [],
      synced: true,
    }).kind).toBe('read_only')
    expect(buildEditorCollaborationStatusModel({
      access: 'edit',
      active: true,
      canEdit: false,
      connectionStatus: 'incompatible',
      durabilityStatus: 'idle',
      participants: [],
      synced: false,
    }).kind).toBe('reload_required')
    // A rejected origin must not borrow the update label: the remedy is an
    // address, not a client version.
    expect(buildEditorCollaborationStatusModel({
      access: 'edit',
      active: true,
      canEdit: false,
      connectionStatus: 'origin_rejected',
      durabilityStatus: 'idle',
      participants: [],
      synced: false,
    }).kind).toBe('origin_rejected')
  })

  it('translates stable collaboration recovery notices without rewriting English', () => {
    const notice =
      'The collaboration lease could not be refreshed; reconnecting read-only.'
    expect(localizeEditorCollaborationNotice(notice, 'de')).toBe(
      'Die Verbindung zum Kollaborationsdienst ist unterbrochen. Inqtrix verbindet sich automatisch erneut.',
    )
    expect(localizeEditorCollaborationNotice(notice, 'en')).toBe(notice)
    expect(localizeEditorCollaborationNotice(
      'Collaboration access changed; revalidating read-only.',
      'de',
    )).toBe(
      'Der Kollaborationszugriff hat sich geändert. Inqtrix prüft die aktuelle Berechtigung erneut.',
    )
  })

  it('holds initiating-user publication focus until the patch arrives, then consumes it once', () => {
    const pending = beginCollaborationPublicationFocus('doc-1', 'patch-b')
    expect(consumeCollaborationPublicationFocus(pending, 'doc-1', [changes[0]!]))
      .toEqual({ focusId: null, pending })

    const arrived = consumeCollaborationPublicationFocus(pending, 'doc-1', changes)
    expect(arrived).toEqual({ focusId: 'patch-b', pending: null })
    expect(consumeCollaborationPublicationFocus(arrived.pending, 'doc-1', changes))
      .toEqual({ focusId: null, pending: null })
    expect(consumeCollaborationPublicationFocus(null, 'doc-1', changes))
      .toEqual({ focusId: null, pending: null })
  })

  it('keeps an A publication pending without switching UI while B is active', () => {
    const pendingByDocument = registerCollaborationPublicationFocus({}, 'doc-a', 'patch-a')
    const pending = pendingCollaborationPublicationFocusForDocument(
      pendingByDocument,
      'doc-a',
    )!

    expect(isCollaborationPublicationFocusCurrent(pending, 'doc-b')).toBe(false)
    expect(pendingCollaborationPublicationFocusForDocument(pendingByDocument, 'doc-b')).toBeNull()
    expect(consumeCollaborationPublicationFocus(pending, 'doc-b', [{
      ...changes[0]!,
      id: 'patch-a',
    }])).toEqual({ focusId: null, pending })
  })

  it('locks write mode to the granted permission', () => {
    expect(effectiveEditorWriteMode('edit', true, 'suggest')).toBe('suggest')
    expect(effectiveEditorWriteMode('edit', true, 'comment')).toBe('comment')
    expect(effectiveEditorWriteMode('suggest', true, 'edit')).toBe('suggest')
    expect(effectiveEditorWriteMode('suggest', true, 'comment')).toBe('comment')
    expect(effectiveEditorWriteMode('view', false, 'edit')).toBe('view')
    expect(effectiveEditorWriteMode('view', false, 'comment')).toBe('view')
  })

  it('keeps shared documents outside the owner hierarchy', () => {
    type DocumentFixture = {
      access?: { mode: 'owner' | 'shared' }
      folderId?: string
      id: string
    }
    const owned: DocumentFixture = { id: 'owned' }
    const shared: DocumentFixture = {
      access: { mode: 'shared' },
      folderId: 'owner-folder',
      id: 'shared',
    }

    expect(partitionEditorDocumentsByAccess([owned, shared])).toEqual({
      owned: [owned],
      shared: [shared],
    })
    expect(isOwnedEditorDocument(owned)).toBe(true)
    expect(isOwnedEditorDocument(shared)).toBe(false)
  })

  it('classifies a row by whether a details page exists for it', () => {
    // Private: no details page — the row must not offer a details action.
    expect(editorDocumentRowKind({ contentMode: 'markdown' })).toBe('owned-private')
    // Shared out by me: details (access, activity) exist.
    expect(editorDocumentRowKind({ contentMode: 'collaboration' })).toBe('owned-shared')
    // Shared with me: details exist and the owner is someone else.
    expect(editorDocumentRowKind({ access: { mode: 'shared' }, contentMode: 'collaboration' })).toBe('shared-with-me')
    expect(editorDocumentRowKind({ access: { mode: 'shared' } })).toBe('shared-with-me')
    // A local/legacy record carries neither fact and reads as private.
    expect(editorDocumentRowKind({})).toBe('owned-private')
  })

  it('derives the full rail grammar from the same ownership classification', () => {
    expect(editorDocumentRailCapabilities({ contentMode: 'markdown' })).toEqual({
      canDelete: true,
      canDrag: true,
      canOpenDetails: false,
      canPin: true,
      canRename: true,
      leadingRole: 'file',
    })
    expect(editorDocumentRailCapabilities({ contentMode: 'collaboration' }))
      .toMatchObject({
        canDelete: true,
        canOpenDetails: true,
        canPin: true,
        leadingRole: 'people',
      })
    expect(editorDocumentRailCapabilities({ access: { mode: 'shared' } }))
      .toEqual({
        canDelete: false,
        canDrag: false,
        canOpenDetails: true,
        canPin: false,
        canRename: false,
        leadingRole: 'people',
      })
  })
})

describe('editor overlay policy', () => {
  it('uses final display outside Changes and restores the chosen display on return', () => {
    const base = {
      collaboration: true,
      changesView: 'open' as const,
      display: 'original' as const,
      documentId: 'doc-1',
      selectedChangeId: 'patch-a',
      visibleChanges: changes,
      writeAuthorId: 'ada',
      writeMode: 'suggest' as const,
    }

    expect(collaborationEditorPolicyUpdate({ ...base, inspectorTab: 'assistant' }))
      .toMatchObject({
        display: 'final',
        selectedSuggestionIds: [],
        visibleSuggestionIds: undefined,
      })
    expect(collaborationEditorPolicyUpdate({ ...base, inspectorTab: 'changes' }))
      .toMatchObject({
        display: 'original',
        selectedSuggestionIds: ['suggestion-a'],
        visibleSuggestionIds: ['suggestion-a', 'suggestion-b'],
      })
  })

  it('activates every suggestion in a selected compound patch', () => {
    const compound = {
      ...changes[0]!,
      suggestionIds: ['delete-a', 'insert-a', 'modify-a'],
    }

    expect(collaborationEditorPolicyUpdate({
      changesView: 'open',
      collaboration: true,
      display: 'simple',
      documentId: 'doc-1',
      inspectorTab: 'changes',
      selectedChangeId: compound.id,
      visibleChanges: [compound],
      writeAuthorId: 'ada',
      writeMode: 'suggest',
    })).toMatchObject({
      selectedSuggestionIds: ['delete-a', 'insert-a', 'modify-a'],
      visibleSuggestionIds: ['delete-a', 'insert-a', 'modify-a'],
    })
  })
})

describe('startupPresentation', () => {
  it('presents both startup transients as one calm syncing state', () => {
    expect(startupPresentation('inactive', 100, true)).toEqual({ calm: true, kind: 'syncing' })
    expect(startupPresentation('syncing', 100, true)).toEqual({ calm: true, kind: 'syncing' })
  })

  it('lets a persisting transient earn its real color after the grace window', () => {
    expect(startupPresentation('syncing', 1_500, true)).toEqual({ calm: false, kind: 'syncing' })
    expect(startupPresentation('inactive', 1_500, true)).toEqual({ calm: false, kind: 'inactive' })
  })

  it('never calms an exceptional state, not even at t=0', () => {
    // The grace exists for expected transients only. An error, a revocation
    // or a forced update must be loud immediately — calming those would be
    // the silent fallback the project forbids.
    for (const kind of ['error', 'access_revoked', 'reload_required', 'origin_rejected', 'read_only'] as const) {
      expect(startupPresentation(kind, 0, true)).toEqual({ calm: false, kind })
    }
  })

  it('leaves the settled states untouched', () => {
    expect(startupPresentation('saved', 0, true)).toEqual({ calm: false, kind: 'saved' })
    expect(startupPresentation('saving', 0, true)).toEqual({ calm: false, kind: 'saving' })
  })

  it('never dresses a local document up as syncing', () => {
    // A pure markdown document is FINAL `inactive` ("Lokal"): no session is
    // coming, so the grace must not show 1.2s of "syncing" and then swap the
    // label — that was itself a startup flash (found in the header review).
    expect(startupPresentation('inactive', 0, false)).toEqual({ calm: false, kind: 'inactive' })
    expect(startupPresentation('inactive', 100, false)).toEqual({ calm: false, kind: 'inactive' })
  })
})

describe('collaboration notice texts', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('has a German text for every notice the controller can publish', () => {
    const codes = Object.keys(COLLABORATION_NOTICES).sort()
    expect(Object.keys(GERMAN_COLLABORATION_NOTICES).sort()).toEqual(codes)
    for (const code of codes as Array<keyof typeof COLLABORATION_NOTICES>) {
      const english = COLLABORATION_NOTICES[code]
      const german = GERMAN_COLLABORATION_NOTICES[code]
      expect(german.trim(), code).not.toBe('')
      expect(german, code).not.toBe(english)
      expect(localizeEditorCollaborationNotice(english, 'de'), code).toBe(german)
      expect(localizeEditorCollaborationNotice(english, 'en'), code).toBe(english)
    }
  })

  it('names every notice text by exactly one code', () => {
    const texts = Object.values(COLLABORATION_NOTICES)
    expect(new Set(texts).size).toBe(texts.length)
    for (const [code, text] of Object.entries(COLLABORATION_NOTICES)) {
      expect(collaborationNoticeCode(text)).toBe(code)
    }
    expect(collaborationNoticeCode('An unpublished sentence.')).toBeNull()
  })

  it('shows a text without a code untranslated and says so in the log', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined)
    const unknown = 'An unpublished sentence.' as unknown as CollaborationNotice

    expect(localizeEditorCollaborationNotice(unknown, 'de')).toBe('An unpublished sentence.')
    expect(warn).toHaveBeenCalledOnce()
  })
})

describe('collaboration edit refusal texts', () => {
  it('has a text in both languages for every reason the editor can report', () => {
    const reasons = [...COLLABORATION_EDIT_REFUSAL_REASONS].sort()
    expect(Object.keys(COLLABORATION_EDIT_REFUSAL_COPY).sort()).toEqual(reasons)
    for (const reason of reasons) {
      const copy = COLLABORATION_EDIT_REFUSAL_COPY[reason]
      expect(copy.de.trim(), reason).not.toBe('')
      expect(copy.en.trim(), reason).not.toBe('')
      expect(copy.de, reason).not.toBe(copy.en)
      expect(localizeCollaborationEditRefusal({ message: 'technical', reason }, 'de'), reason)
        .toBe(copy.de)
      expect(localizeCollaborationEditRefusal({ message: 'technical', reason }, 'en'), reason)
        .toBe(copy.en)
    }
    expect(localizeCollaborationEditRefusal(null, 'de')).toBeNull()
  })

  it('covers every reason of the suggestion rules in the editor schema', () => {
    for (const reason of UNSUPPORTED_SUGGESTION_REASONS) {
      expect(COLLABORATION_EDIT_REFUSAL_REASONS).toContain(reason)
    }
  })
})

describe('calm saving status', () => {
  afterEach(() => {
    vi.useRealTimers()
  })

  /** A status driven the way the surface drives it: every change of the
   * reported kind or of the oldest unconfirmed change is an update at a
   * point in time. Records every change of the shown kind with its time. */
  function statusFrom(initial: EditorCollaborationStatusKind = 'saved') {
    vi.useFakeTimers()
    vi.setSystemTime(0)
    const shown: Array<[number, EditorCollaborationStatusKind]> = []
    const status = new CalmCollaborationStatus(initial, null)
    status.listen((kind) => shown.push([Date.now(), kind]))
    const until = (time: number) => vi.advanceTimersByTime(time - Date.now())
    const at = (
      time: number,
      kind: EditorCollaborationStatusKind,
      unconfirmedSince: number | null,
    ) => {
      until(time)
      status.update(kind, unconfirmedSince)
    }
    return { at, shown, status, until }
  }

  it('keeps "Gespeichert" while a writer types for ten seconds and every change is confirmed within fractions of a second', () => {
    const { at, shown, status } = statusFrom()
    // A keystroke every 100 ms; the oldest unconfirmed change is never older
    // than 300 ms.
    for (let time = 0; time <= 10_000; time += 100) {
      at(time, 'saving', Math.max(0, time - 300))
    }
    at(10_400, 'saved', null)

    expect(shown).toEqual([])
    expect(status.kind).toBe('saved')
  })

  it('shows "Wird gespeichert" once a change has waited 2 s and keeps it until everything is confirmed', () => {
    const { at, shown, until } = statusFrom()
    at(0, 'saving', 0)
    until(1_999)
    expect(shown).toEqual([])
    until(2_000)
    expect(shown).toEqual([[2_000, 'saving']])

    // The slow change is confirmed while a younger one still waits.
    at(2_600, 'saving', 2_500)
    until(4_000)
    expect(shown).toEqual([[2_000, 'saving']])
    at(4_100, 'saved', null)
    expect(shown).toEqual([[2_000, 'saving'], [4_100, 'saved']])
  })

  it('counts from the oldest unconfirmed change, not from when the status first saw one', () => {
    const { at, shown, until } = statusFrom()
    at(0, 'saving', 0)
    // The first change is confirmed; one made at 1 400 still waits.
    at(1_500, 'saving', 1_400)
    until(3_399)
    expect(shown).toEqual([])
    until(3_400)
    expect(shown).toEqual([[3_400, 'saving']])
  })

  it('counts from when it first saw a change unconfirmed when the source reports no time', () => {
    const { at, shown, until } = statusFrom()
    at(500, 'saving', null)
    until(2_499)
    expect(shown).toEqual([])
    until(2_500)
    expect(shown).toEqual([[2_500, 'saving']])
  })

  it('reaches "Gespeichert" at the confirmation, after "Wird gespeichert" was visible for half a second', () => {
    const { at, shown, until } = statusFrom()
    at(0, 'saving', 0)
    until(2_000)
    at(2_100, 'saved', null)
    until(2_499)
    expect(shown).toEqual([[2_000, 'saving']])
    until(2_500)
    expect(shown).toEqual([[2_000, 'saving'], [2_500, 'saved']])
  })

  it('says "Wird gespeichert" after a delay until everything is confirmed, however young the change that still waits', () => {
    const { at, shown } = statusFrom()
    at(0, 'saving', 0)
    at(10_000, 'saving_delayed', 0)
    // The late change is confirmed; one made half a second ago still waits.
    at(11_000, 'saving', 10_500)
    at(12_400, 'saving', 10_500)
    expect(shown).toEqual([[2_000, 'saving'], [10_000, 'saving_delayed'], [11_000, 'saving']])
    at(12_600, 'saved', null)
    expect(shown.at(-1)).toEqual([12_600, 'saved'])
  })

  it('shows a reconnect, an error and a read-only state at once', () => {
    const { at, shown } = statusFrom()
    at(100, 'reconnecting', 0)
    expect(shown).toEqual([[100, 'reconnecting']])
    at(3_000, 'saving', 0)
    expect(shown.at(-1)).toEqual([3_000, 'saving'])
    at(3_100, 'error', 0)
    expect(shown.at(-1)).toEqual([3_100, 'error'])
    at(3_200, 'read_only', null)
    expect(shown.at(-1)).toEqual([3_200, 'read_only'])
  })
})
