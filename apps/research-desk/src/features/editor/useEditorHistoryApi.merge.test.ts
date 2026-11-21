import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  deleteEditorComment,
  deleteEditorDocument,
  deleteEditorFolder,
  getEditorDocument,
  listEditorComments,
  listEditorDocuments,
  listEditorFolders,
  patchEditorDocumentMetadata,
  saveEditorComments,
  saveEditorDocument,
  saveEditorFolder,
  type ServerEditorDocument,
} from '@/api/inqtrixClient'
import { createEmptyProjectState } from '@/features/project/seedProject'
import type { ProjectState } from '@/features/project/types'
import { researchDeskReducer, type ResearchDeskAction } from '@/features/researchDesk/state'
import { documentRecordFromServer } from './editorSync'
import {
  AUTOSAVE_DEBOUNCE_MS,
  useEditorHistoryApi,
  type EditorHistoryApiHandle,
} from './useEditorHistoryApi'

// The hook runs for real on a small stand-in for React's render cycle: hook
// slots keep their values across renders, a state update or a dispatch
// renders again in a microtask, and an effect runs after the render when it
// is new or one of its dependencies changed. No DOM in this lane.
const react = vi.hoisted(() => {
  type Slot = {
    cleanup?: unknown
    deps?: readonly unknown[]
    effect?: () => unknown
    filled?: boolean
    pending?: boolean
    setter?: (update: unknown) => void
    value?: unknown
  }
  const cycle = {
    component: null as (() => void) | null,
    cursor: 0,
    scheduled: false,
    slots: [] as Slot[],
  }
  function slot(): Slot {
    const index = cycle.cursor
    cycle.cursor += 1
    cycle.slots[index] ??= {}
    return cycle.slots[index]
  }
  function depsChanged(
    previous: readonly unknown[] | undefined,
    next: readonly unknown[] | undefined,
  ): boolean {
    return !previous
      || !next
      || previous.length !== next.length
      || next.some((value, index) => !Object.is(value, previous[index]))
  }
  function runCleanup(entry: Slot): void {
    if (typeof entry.cleanup === 'function') (entry.cleanup as () => void)()
  }
  function render(): void {
    if (!cycle.component) return
    cycle.cursor = 0
    cycle.component()
    const due = cycle.slots.filter((entry) => entry.pending)
    for (const entry of due) runCleanup(entry)
    for (const entry of due) {
      entry.pending = false
      entry.cleanup = entry.effect?.()
    }
  }
  function scheduleRender(): void {
    if (cycle.scheduled) return
    cycle.scheduled = true
    queueMicrotask(() => {
      cycle.scheduled = false
      render()
    })
  }
  function useMemo<T>(factory: () => T, deps?: readonly unknown[]): T {
    const entry = slot()
    if (!entry.filled || depsChanged(entry.deps, deps)) {
      entry.value = factory()
      entry.deps = deps
      entry.filled = true
    }
    return entry.value as T
  }
  return {
    mount(component: () => void): void {
      cycle.component = component
      cycle.slots = []
      render()
    },
    scheduleRender,
    unmount(): void {
      for (const entry of cycle.slots) runCleanup(entry)
      cycle.component = null
      cycle.slots = []
    },
    useCallback<T>(callback: T, deps?: readonly unknown[]): T {
      return useMemo(() => callback, deps)
    },
    useEffect(effect: () => unknown, deps?: readonly unknown[]): void {
      const entry = slot()
      if (!entry.filled || depsChanged(entry.deps, deps)) {
        entry.effect = effect
        entry.deps = deps
        entry.filled = true
        entry.pending = true
      }
    },
    useMemo,
    useRef<T>(initial: T): { current: T } {
      const entry = slot()
      if (!entry.filled) {
        entry.value = { current: initial }
        entry.filled = true
      }
      return entry.value as { current: T }
    },
    useState<T>(initial: T | (() => T)): [T, (update: T | ((current: T) => T)) => void] {
      const entry = slot()
      if (!entry.filled) {
        entry.value = typeof initial === 'function' ? (initial as () => T)() : initial
        entry.filled = true
        entry.setter = (update) => {
          const next = typeof update === 'function'
            ? (update as (current: unknown) => unknown)(entry.value)
            : update
          if (Object.is(next, entry.value)) return
          entry.value = next
          scheduleRender()
        }
      }
      return [entry.value as T, entry.setter as (update: T | ((current: T) => T)) => void]
    },
  }
})

vi.mock('react', async (original) => ({
  ...await original<typeof import('react')>(),
  useCallback: react.useCallback,
  useEffect: react.useEffect,
  useMemo: react.useMemo,
  useRef: react.useRef,
  useState: react.useState,
}))

vi.mock('@/api/inqtrixClient', async (original) => ({
  ...await original<typeof import('@/api/inqtrixClient')>(),
  deleteEditorComment: vi.fn(),
  deleteEditorDocument: vi.fn(),
  deleteEditorFolder: vi.fn(),
  getEditorDocument: vi.fn(),
  listEditorComments: vi.fn(),
  listEditorDocuments: vi.fn(),
  listEditorFolders: vi.fn(),
  patchEditorDocumentMetadata: vi.fn(),
  saveEditorComments: vi.fn(),
  saveEditorDocument: vi.fn(),
  saveEditorFolder: vi.fn(),
}))

type SavePayload = Parameters<typeof saveEditorDocument>[1]
type MetadataPayload = Parameters<typeof patchEditorDocumentMetadata>[1]

function httpError(status: number, detail: Record<string, unknown> = {}): Error {
  return Object.assign(new Error(`HTTP ${status}`), { detail, status })
}

/** The editor endpoints over one in-memory store, with the server's revision
 * guard: a save is taken only on top of the revision the store holds. */
class FakeEditorServer {
  readonly documents = new Map<string, ServerEditorDocument>()
  /** Every write and how it ended, in order. */
  readonly writes: string[] = []
  private holds: Array<{ matches: (request: string) => boolean; released: Promise<void> }> = []

  add(id: string, contentMarkdown: string): void {
    const now = Date.now() / 1000
    this.documents.set(id, {
      access: { mode: 'owner', permission: 'edit' },
      content_markdown: contentMarkdown,
      content_mode: 'markdown',
      created_at: now,
      diff_anchor_markdown: null,
      diff_anchor_updated_at: null,
      folder_id: null,
      id,
      metadata_revision: 1,
      revision: 1,
      source: 'blank',
      source_run_id: null,
      title: 'Plan.md',
      updated_at: now,
    })
  }

  /** Another tab or writer saves a new text. */
  writeElsewhere(id: string, contentMarkdown: string): void {
    const stored = this.stored(id)
    this.documents.set(id, {
      ...stored,
      content_markdown: contentMarkdown,
      revision: stored.revision + 1,
      updated_at: Date.now() / 1000 + 1,
    })
  }

  /** The operator restores a database backup: the document stands on an
   * older revision again, with that revision's text. */
  restoreBackup(id: string, contentMarkdown: string, revision: number): void {
    const stored = this.stored(id)
    this.documents.set(id, { ...stored, content_markdown: contentMarkdown, revision })
  }

  /** Another tab renames the document; its text and revision stay. */
  renameElsewhere(id: string, title: string): void {
    const stored = this.stored(id)
    this.documents.set(id, {
      ...stored,
      metadata_revision: (stored.metadata_revision ?? 1) + 1,
      title,
      updated_at: Date.now() / 1000 + 1,
    })
  }

  /** Another tab converts the document to collaboration. */
  convertElsewhere(id: string): void {
    const stored = this.stored(id)
    this.documents.set(id, {
      ...stored,
      collaboration: {
        generation: 1,
        persisted_sequence: 0,
        projection_sequence: 0,
        projection_updated_at: null,
        schema_version: 1,
      },
      content_mode: 'collaboration',
      metadata_revision: (stored.metadata_revision ?? 1) + 1,
      updated_at: Date.now() / 1000 + 1,
    })
  }

  text(id: string): string | undefined {
    return this.documents.get(id)?.content_markdown
  }

  /** Holds the next request `matches` names until the returned release runs. */
  hold(matches: (request: string) => boolean): () => void {
    let release: () => void = () => undefined
    const released = new Promise<void>((resolve) => {
      release = resolve
    })
    this.holds.push({ matches, released })
    return release
  }

  async list(): Promise<{ data: ServerEditorDocument[]; next_cursor: null }> {
    const data = [...this.documents.values()].map((document) => {
      const row: ServerEditorDocument = { ...document }
      delete row.content_markdown
      return row
    })
    return { data, next_cursor: null }
  }

  async get(id: string): Promise<ServerEditorDocument> {
    await this.pass(`GET ${id}`)
    return structuredClone(this.stored(id))
  }

  async save(id: string, payload: SavePayload): Promise<ServerEditorDocument> {
    await this.pass(`PUT ${id}`)
    const stored = this.documents.get(id)
    if (stored?.content_mode === 'collaboration') {
      this.writes.push(`PUT ${id} refused: content_mode_changed`)
      throw httpError(409, { reason: 'content_mode_changed' })
    }
    if (stored && stored.revision !== payload.revision - 1) {
      this.writes.push(`PUT ${id} refused: revision ${payload.revision}`)
      throw httpError(409, { current_revision: stored.revision })
    }
    const saved: ServerEditorDocument = {
      access: { mode: 'owner', permission: 'edit' },
      content_mode: 'markdown',
      metadata_revision: stored?.metadata_revision ?? 1,
      ...payload,
      id,
    }
    this.documents.set(id, saved)
    this.writes.push(`PUT ${id} r${payload.revision}`)
    return structuredClone(saved)
  }

  async patchMetadata(id: string, payload: MetadataPayload): Promise<ServerEditorDocument> {
    const stored = this.stored(id)
    const saved: ServerEditorDocument = {
      ...stored,
      folder_id: payload.folder_id === undefined ? stored.folder_id : payload.folder_id,
      metadata_revision: (stored.metadata_revision ?? 1) + 1,
      title: payload.title ?? stored.title,
    }
    this.documents.set(id, saved)
    this.writes.push(`PATCH ${id}`)
    return structuredClone(saved)
  }

  async remove(id: string): Promise<void> {
    this.writes.push(`DELETE ${id}`)
    this.documents.delete(id)
  }

  private async pass(request: string): Promise<void> {
    const index = this.holds.findIndex((hold) => hold.matches(request))
    if (index === -1) return
    const [hold] = this.holds.splice(index, 1)
    await hold.released
  }

  private stored(id: string): ServerEditorDocument {
    const stored = this.documents.get(id)
    if (!stored) throw httpError(404)
    return stored
  }
}

type Desk = {
  readonly handle: EditorHistoryApiHandle
  readonly state: ProjectState
  refresh: () => void
  select: (documentId: string) => void
  /** Types a new text into the open document a second after the last step,
   * in an editor that showed the record's text. */
  type: (contentMarkdown: string) => void
  /** Types a new text into an editor that still showed `baseContentMarkdown`. */
  typeOn: (baseContentMarkdown: string, contentMarkdown: string) => void
}

function mountDesk({
  initial = createEmptyProjectState(),
  syncActive = true,
}: { initial?: ProjectState; syncActive?: boolean } = {}): Desk {
  let state = initial
  let selectedDocumentId: string | null = null
  let refreshToken = 0
  let handle: EditorHistoryApiHandle | null = null
  const dispatch = (action: ResearchDeskAction) => {
    state = researchDeskReducer(state, action)
    react.scheduleRender()
  }
  react.mount(() => {
    handle = useEditorHistoryApi({
      apiKey: undefined,
      dispatch,
      editorCommentOutbox: state.editorCommentOutbox,
      editorComments: state.editorComments,
      editorDocuments: state.editorDocuments,
      editorFolders: state.editorFolders,
      locale: 'de',
      projectEpoch: state.projectEpoch,
      refreshToken,
      selectedDocumentId,
      syncActive,
      workspaceId: 'workspace-test',
    })
  })
  return {
    get handle() {
      if (!handle) throw new Error('The hook has not rendered.')
      return handle
    },
    get state() {
      return state
    },
    refresh() {
      refreshToken += 1
      react.scheduleRender()
    },
    select(documentId) {
      selectedDocumentId = documentId
      react.scheduleRender()
    },
    type(contentMarkdown) {
      this.typeOn(state.editorDocuments['doc-1'].contentMarkdown, contentMarkdown)
    },
    typeOn(baseContentMarkdown, contentMarkdown) {
      vi.setSystemTime(Date.now() + 1_000)
      dispatch({
        baseContentMarkdown,
        contentMarkdown,
        documentId: 'doc-1',
        type: 'updateEditorDocumentMarkdown',
      } as ResearchDeskAction)
    },
  }
}

/** The page's `beforeunload` listeners, as the hook registers them. */
const pageListeners = new Map<string, Set<(event: unknown) => void>>()

/** Whether leaving the page now asks the user first. */
function leavingAsks(): boolean {
  const event = {
    defaultPrevented: false,
    preventDefault() {
      this.defaultPrevented = true
    },
    returnValue: undefined as unknown,
  }
  for (const listener of pageListeners.get('beforeunload') ?? []) listener(event)
  return event.defaultPrevented
}

/** Lets every pending promise, render and effect run, without moving the clock. */
async function settle(): Promise<void> {
  for (let round = 0; round < 12; round += 1) {
    await new Promise<void>((resolve) => {
      setImmediate(resolve)
    })
  }
}

async function passTime(milliseconds: number): Promise<void> {
  await vi.advanceTimersByTimeAsync(milliseconds)
  await settle()
}

/** Runs a call of the hook to its end, moving the clock in small steps for
 * the waits of the save lock. */
async function drive<T>(work: Promise<T>): Promise<T> {
  let done = false
  work.then(() => {
    done = true
  }, () => {
    done = true
  })
  for (let step = 0; step < 40 && !done; step += 1) await passTime(10)
  return work
}

function paragraphs(...lines: string[]): string {
  return `${lines.join('\n\n')}\n`
}

const SAVED = paragraphs('# Plan', 'Absatz eins.', 'Absatz zwei.', 'Absatz drei.')

let server: FakeEditorServer

async function openedDesk(contentMarkdown = SAVED): Promise<Desk> {
  server.add('doc-1', contentMarkdown)
  const desk = mountDesk()
  await settle()
  desk.select('doc-1')
  await settle()
  expect(desk.state.editorDocuments['doc-1'].contentMarkdown).toBe(contentMarkdown)
  return desk
}

function deletes(): string[] {
  return server.writes.filter((write) => write.startsWith('DELETE'))
}

beforeEach(() => {
  vi.useFakeTimers({
    now: new Date('2026-03-04T08:00:00.000Z'),
    toFake: ['Date', 'setTimeout', 'clearTimeout'],
  })
  vi.stubGlobal('window', globalThis)
  pageListeners.clear()
  vi.stubGlobal('addEventListener', (type: string, listener: (event: unknown) => void) => {
    const listeners = pageListeners.get(type) ?? new Set()
    listeners.add(listener)
    pageListeners.set(type, listeners)
  })
  vi.stubGlobal('removeEventListener', (type: string, listener: (event: unknown) => void) => {
    pageListeners.get(type)?.delete(listener)
  })
  vi.spyOn(console, 'warn').mockImplementation(() => undefined)
  server = new FakeEditorServer()
  vi.mocked(listEditorFolders).mockReset().mockResolvedValue([])
  vi.mocked(listEditorDocuments).mockReset().mockImplementation(() => server.list())
  vi.mocked(getEditorDocument).mockReset().mockImplementation((id) => server.get(id))
  vi.mocked(saveEditorDocument).mockReset().mockImplementation((id, payload) => server.save(id, payload))
  vi.mocked(patchEditorDocumentMetadata).mockReset()
    .mockImplementation((id, payload) => server.patchMetadata(id, payload))
  vi.mocked(deleteEditorDocument).mockReset().mockImplementation((id) => server.remove(id))
  vi.mocked(listEditorComments).mockReset().mockResolvedValue({ data: [], next_cursor: null })
  vi.mocked(saveEditorComments).mockReset()
  vi.mocked(deleteEditorComment).mockReset()
  vi.mocked(saveEditorFolder).mockReset()
  vi.mocked(deleteEditorFolder).mockReset()
})

afterEach(() => {
  react.unmount()
  vi.useRealTimers()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('a save that lost against another writer', () => {
  it('merges a change in another paragraph and saves both on the new revision', async () => {
    const desk = await openedDesk()
    desk.type(SAVED.replace('Absatz eins.', 'Absatz eins, hier.'))
    server.writeElsewhere('doc-1', SAVED.replace('Absatz drei.', 'Absatz drei, dort.'))

    await passTime(AUTOSAVE_DEBOUNCE_MS)
    await passTime(AUTOSAVE_DEBOUNCE_MS)

    const both = paragraphs('# Plan', 'Absatz eins, hier.', 'Absatz zwei.', 'Absatz drei, dort.')
    expect(server.writes).toEqual(['PUT doc-1 refused: revision 2', 'PUT doc-1 r3'])
    expect(server.text('doc-1')).toBe(both)
    expect(desk.state.editorDocuments['doc-1']).toMatchObject({ contentMarkdown: both, revision: 3 })
    expect(desk.state.editorDocumentOrder).toEqual(['doc-1'])
  })

  it('saves a conflicting own version as a copy at once and puts the original on the server version', async () => {
    const desk = await openedDesk()
    const own = SAVED.replace('Absatz zwei.', 'Absatz zwei, hier.')
    const elsewhere = SAVED.replace('Absatz zwei.', 'Absatz zwei, dort.')
    desk.type(own)
    server.writeElsewhere('doc-1', elsewhere)

    await passTime(AUTOSAVE_DEBOUNCE_MS)

    const copyId = desk.state.editorDocumentOrder[0]
    expect(desk.state.editorDocumentOrder).toEqual([copyId, 'doc-1'])
    // The copy reached the server in the same pass, before any further
    // autosave delay ran out.
    expect(server.writes).toEqual(['PUT doc-1 refused: revision 2', `PUT ${copyId} r1`])
    expect(server.text(copyId)).toBe(own)
    expect(desk.state.editorDocuments[copyId].title)
      .toMatch(/^Plan \(Konfliktkopie \d{2}\.\d{2}\.\d{4} \d{2}:\d{2}:\d{2}\)\.md$/)
    expect(desk.state.editorDocuments['doc-1']).toMatchObject({ contentMarkdown: elsewhere, revision: 2 })
    expect(desk.handle.conflictCopies).toEqual([{
      copyDocumentId: copyId,
      originalDocumentId: 'doc-1',
      reason: 'concurrent_change',
      saved: true,
    }])
  })

  it('keeps own text in a copy when the server stands behind the record, as after a restored backup', async () => {
    const desk = await openedDesk()
    const own = SAVED.replace('Absatz zwei.', 'Absatz zwei, hier.')
    desk.type(own)
    await passTime(AUTOSAVE_DEBOUNCE_MS)
    const restored = SAVED.replace('Absatz eins.', 'Absatz eins, alt.')
    server.restoreBackup('doc-1', restored, 1)
    const later = own.replace('Absatz drei.', 'Absatz drei, später.')
    desk.type(later)

    await passTime(AUTOSAVE_DEBOUNCE_MS)

    // Nothing is marked saved that the server never took: the text typed
    // after the restore is in a copy on the server, the original stands on
    // the server's revision, and the next save goes on top of it.
    const copyId = desk.state.editorDocumentOrder[0]
    expect(desk.state.editorDocumentOrder).toEqual([copyId, 'doc-1'])
    expect(server.writes).toEqual(['PUT doc-1 r2', 'PUT doc-1 refused: revision 3', `PUT ${copyId} r1`])
    expect(server.text(copyId)).toBe(later)
    expect(desk.state.editorDocuments['doc-1']).toMatchObject({ contentMarkdown: restored, revision: 1 })
  })

  it('sends no delete request when a conflict leaves a copy, also on the autosave passes after it', async () => {
    const desk = await openedDesk()
    desk.type(SAVED.replace('Absatz zwei.', 'Absatz zwei, hier.'))
    server.writeElsewhere('doc-1', SAVED.replace('Absatz zwei.', 'Absatz zwei, dort.'))
    await passTime(AUTOSAVE_DEBOUNCE_MS)
    const copyId = desk.state.editorDocumentOrder[0]

    desk.type(desk.state.editorDocuments['doc-1'].contentMarkdown.replace('Absatz drei.', 'Absatz drei, weiter.'))
    await passTime(AUTOSAVE_DEBOUNCE_MS)
    await passTime(AUTOSAVE_DEBOUNCE_MS)

    expect(deletes()).toEqual([])
    expect(server.documents.has('doc-1')).toBe(true)
    expect(server.documents.has(copyId)).toBe(true)
    expect(server.text('doc-1')).toBe(paragraphs('# Plan', 'Absatz eins.', 'Absatz zwei, dort.', 'Absatz drei, weiter.'))
  })

  it('keeps keystrokes typed while a save ran out of the base, so a later merge keeps them', async () => {
    const desk = await openedDesk()
    const sent = SAVED.replace('Absatz eins.', 'Absatz eins, a.')
    desk.type(sent)
    const release = server.hold((request) => request === 'PUT doc-1')
    await passTime(AUTOSAVE_DEBOUNCE_MS)
    const typed = SAVED.replace('Absatz eins.', 'Absatz eins, ab.')
    desk.type(typed)
    release()
    await settle()
    expect(server.writes).toEqual(['PUT doc-1 r2'])

    server.writeElsewhere('doc-1', sent.replace('Absatz drei.', 'Absatz drei, dort.'))
    await passTime(AUTOSAVE_DEBOUNCE_MS)
    await passTime(AUTOSAVE_DEBOUNCE_MS)

    expect(server.text('doc-1')).toBe(typed.replace('Absatz drei.', 'Absatz drei, dort.'))
    expect(server.writes).toEqual(['PUT doc-1 r2', 'PUT doc-1 refused: revision 3', 'PUT doc-1 r4'])
  })

  it('keeps own text in a copy when the document was converted to collaboration elsewhere, and switches the original', async () => {
    const desk = await openedDesk()
    const own = SAVED.replace('Absatz eins.', 'Absatz eins, hier.')
    desk.type(own)
    server.convertElsewhere('doc-1')

    await passTime(AUTOSAVE_DEBOUNCE_MS)

    const copyId = desk.state.editorDocumentOrder[0]
    expect(desk.state.editorDocumentOrder).toEqual([copyId, 'doc-1'])
    expect(desk.state.editorDocuments['doc-1'].contentMode).toBe('collaboration')
    expect(server.writes).toEqual(['PUT doc-1 refused: content_mode_changed', `PUT ${copyId} r1`])
    expect(server.text(copyId)).toBe(own)
    expect(desk.handle.conflictCopies).toEqual([{
      copyDocumentId: copyId,
      originalDocumentId: 'doc-1',
      reason: 'content_mode_changed',
      saved: true,
    }])
  })
})

describe('a server version seen without a save', () => {
  it('merges a newer server text into a loaded document when the lists refresh, without a save', async () => {
    const desk = await openedDesk()
    const elsewhere = SAVED.replace('Absatz zwei.', 'Absatz zwei, dort.')
    server.writeElsewhere('doc-1', elsewhere)

    desk.refresh()
    await settle()

    expect(desk.state.editorDocuments['doc-1']).toMatchObject({ contentMarkdown: elsewhere, revision: 2 })
    await passTime(AUTOSAVE_DEBOUNCE_MS)
    expect(server.writes).toEqual([])
    // The next own change goes onto that revision without a refusal.
    desk.type(elsewhere.replace('Absatz eins.', 'Absatz eins, hier.'))
    await passTime(AUTOSAVE_DEBOUNCE_MS)
    expect(server.writes).toEqual(['PUT doc-1 r3'])
  })

  it('merges unsaved own text with a newer server text when the lists refresh, and saves the result', async () => {
    const desk = await openedDesk()
    desk.type(SAVED.replace('Absatz eins.', 'Absatz eins, hier.'))
    server.writeElsewhere('doc-1', SAVED.replace('Absatz drei.', 'Absatz drei, dort.'))

    desk.refresh()
    await settle()
    const both = paragraphs('# Plan', 'Absatz eins, hier.', 'Absatz zwei.', 'Absatz drei, dort.')
    expect(desk.state.editorDocuments['doc-1']).toMatchObject({ contentMarkdown: both, revision: 2 })

    await passTime(AUTOSAVE_DEBOUNCE_MS)
    expect(server.writes).toEqual(['PUT doc-1 r3'])
    expect(server.text('doc-1')).toBe(both)
  })

  it('takes a rename made elsewhere when the lists refresh, and saves nothing', async () => {
    const desk = await openedDesk()
    server.renameElsewhere('doc-1', 'Plan neu.md')

    desk.refresh()
    await settle()

    expect(desk.state.editorDocuments['doc-1']).toMatchObject({
      contentMarkdown: SAVED,
      revision: 1,
      title: 'Plan neu.md',
    })
    await passTime(AUTOSAVE_DEBOUNCE_MS)
    expect(server.writes).toEqual([])
  })

  it('opens a document saved elsewhere after the list with its current revision, so the first save is not refused', async () => {
    server.add('doc-1', SAVED)
    const desk = mountDesk()
    await settle()
    const elsewhere = SAVED.replace('Absatz zwei.', 'Absatz zwei, dort.')
    server.writeElsewhere('doc-1', elsewhere)

    desk.select('doc-1')
    await settle()
    expect(desk.state.editorDocuments['doc-1']).toMatchObject({ contentMarkdown: elsewhere, revision: 2 })
    desk.type(elsewhere.replace('Absatz eins.', 'Absatz eins, hier.'))
    await passTime(AUTOSAVE_DEBOUNCE_MS)

    expect(server.writes).toEqual(['PUT doc-1 r3'])
  })
})

describe('the immediate save of sharing and accepting', () => {
  it('saves once more after a merge, so that sharing goes on from a saved revision', async () => {
    const desk = await openedDesk()
    desk.type(SAVED.replace('Absatz eins.', 'Absatz eins, hier.'))
    server.writeElsewhere('doc-1', SAVED.replace('Absatz drei.', 'Absatz drei, dort.'))

    const outcome = await drive(desk.handle.flushDocumentForShare('doc-1'))

    const both = paragraphs('# Plan', 'Absatz eins, hier.', 'Absatz zwei.', 'Absatz drei, dort.')
    expect(outcome).toMatchObject({ contentMarkdown: both, kind: 'saved', revision: 3 })
    expect(server.writes).toEqual(['PUT doc-1 refused: revision 2', 'PUT doc-1 r3'])
    expect(server.text('doc-1')).toBe(both)
  })

  it('tells sharing that the document was converted elsewhere', async () => {
    const desk = await openedDesk()
    desk.type(SAVED.replace('Absatz eins.', 'Absatz eins, hier.'))
    server.convertElsewhere('doc-1')

    const outcome = await drive(desk.handle.flushDocumentForShare('doc-1'))

    expect(outcome).toEqual({ conflictCopyId: desk.state.editorDocumentOrder[0], kind: 'converted' })
  })

  it('merges a server version handed over after an accepted change and saves a conflict copy at once', async () => {
    const desk = await openedDesk()
    const own = SAVED.replace('Absatz zwei.', 'Absatz zwei, hier.')
    const accepted = SAVED.replace('Absatz zwei.', 'Absatz zwei, angenommen.')
    desk.type(own)
    server.writeElsewhere('doc-1', accepted)
    const version = documentRecordFromServer(server.documents.get('doc-1') as ServerEditorDocument)

    const outcome = await drive(desk.handle.mergeServerDocument('doc-1', version))

    const copyId = desk.state.editorDocumentOrder[0]
    expect(outcome).toEqual({ conflictCopyId: copyId, kind: 'conflict' })
    expect(server.writes).toEqual([`PUT ${copyId} r1`])
    expect(server.text(copyId)).toBe(own)
    expect(desk.state.editorDocuments['doc-1']).toMatchObject({ contentMarkdown: accepted, revision: 2 })
  })

  it('shows a conflict copy as unsaved until the server confirms its first save', async () => {
    const desk = await openedDesk()
    desk.type(SAVED.replace('Absatz zwei.', 'Absatz zwei, hier.'))
    server.writeElsewhere('doc-1', SAVED.replace('Absatz zwei.', 'Absatz zwei, angenommen.'))
    const accepted = documentRecordFromServer(server.documents.get('doc-1') as ServerEditorDocument)
    const release = server.hold((request) => request.startsWith('PUT ') && request !== 'PUT doc-1')

    const merging = desk.handle.mergeServerDocument('doc-1', accepted)
    await settle()
    const copyId = desk.state.editorDocumentOrder[0]
    expect(desk.handle.conflictCopies).toEqual([{
      copyDocumentId: copyId,
      originalDocumentId: 'doc-1',
      reason: 'concurrent_change',
      saved: false,
    }])

    release()
    await drive(merging)
    expect(desk.handle.conflictCopies).toEqual([{
      copyDocumentId: copyId,
      originalDocumentId: 'doc-1',
      reason: 'concurrent_change',
      saved: true,
    }])
  })
})

describe('the editor typing on a record that changed under it', () => {
  it('saves the editor text as a conflict copy at once when it overlaps a version merged meanwhile', async () => {
    const desk = await openedDesk()
    const accepted = SAVED.replace('Absatz zwei.', 'Absatz zwei, angenommen.')
    server.writeElsewhere('doc-1', accepted)
    const version = documentRecordFromServer(server.documents.get('doc-1') as ServerEditorDocument)

    // The merge reaches the record while the editor still shows the saved
    // text, and a keystroke is sent on that text.
    const merging = desk.handle.mergeServerDocument('doc-1', version)
    const typed = SAVED.replace('Absatz zwei.', 'Absatz zwei, getippt.')
    desk.typeOn(SAVED, typed)
    await drive(merging)
    await passTime(100)

    const copyId = desk.state.editorDocumentOrder[0]
    expect(desk.state.editorDocumentOrder).toEqual([copyId, 'doc-1'])
    expect(desk.state.editorDocuments['doc-1']).toMatchObject({ contentMarkdown: accepted, revision: 2 })
    expect(desk.state.editorDocuments['doc-1'].pendingConflictMarkdown).toBeUndefined()
    expect(server.text(copyId)).toBe(typed)
    expect(server.writes).toEqual([`PUT ${copyId} r1`])
    expect(desk.handle.conflictCopies).toEqual([{
      copyDocumentId: copyId,
      originalDocumentId: 'doc-1',
      reason: 'concurrent_change',
      saved: true,
    }])
  })

  it('merges the editor text with a version merged meanwhile in another paragraph, and saves both', async () => {
    const desk = await openedDesk()
    const accepted = SAVED.replace('Absatz drei.', 'Absatz drei, angenommen.')
    server.writeElsewhere('doc-1', accepted)
    const version = documentRecordFromServer(server.documents.get('doc-1') as ServerEditorDocument)

    const merging = desk.handle.mergeServerDocument('doc-1', version)
    desk.typeOn(SAVED, SAVED.replace('Absatz eins.', 'Absatz eins, getippt.'))
    await drive(merging)
    await passTime(AUTOSAVE_DEBOUNCE_MS)

    const both = paragraphs('# Plan', 'Absatz eins, getippt.', 'Absatz zwei.', 'Absatz drei, angenommen.')
    expect(desk.state.editorDocumentOrder).toEqual(['doc-1'])
    expect(server.writes).toEqual(['PUT doc-1 r3'])
    expect(server.text('doc-1')).toBe(both)
  })
})

describe('a conversion elsewhere that the lists show first', () => {
  it('keeps own text in a copy before the document switches to collaboration', async () => {
    const desk = await openedDesk()
    const own = SAVED.replace('Absatz eins.', 'Absatz eins, hier.')
    desk.type(own)
    server.convertElsewhere('doc-1')

    desk.refresh()
    await settle()
    await passTime(100)

    const copyId = desk.state.editorDocumentOrder[0]
    expect(desk.state.editorDocumentOrder).toEqual([copyId, 'doc-1'])
    expect(desk.state.editorDocuments['doc-1'].contentMode).toBe('collaboration')
    expect(server.text(copyId)).toBe(own)
    expect(server.writes).toEqual([`PUT ${copyId} r1`])
    expect(desk.handle.conflictCopies).toEqual([{
      copyDocumentId: copyId,
      originalDocumentId: 'doc-1',
      reason: 'content_mode_changed',
      saved: true,
    }])
    await passTime(AUTOSAVE_DEBOUNCE_MS)
    expect(deletes()).toEqual([])
  })

  it('makes no copy when the document had no own changes', async () => {
    const desk = await openedDesk()
    server.convertElsewhere('doc-1')

    desk.refresh()
    await settle()
    await passTime(AUTOSAVE_DEBOUNCE_MS)

    expect(desk.state.editorDocumentOrder).toEqual(['doc-1'])
    expect(desk.state.editorDocuments['doc-1'].contentMode).toBe('collaboration')
    expect(server.writes).toEqual([])
    expect(desk.handle.conflictCopies).toEqual([])
  })
})

describe('leaving the page while a conflict copy is not saved', () => {
  it('asks until the server confirms the first save of the copy', async () => {
    const desk = await openedDesk()
    desk.type(SAVED.replace('Absatz zwei.', 'Absatz zwei, hier.'))
    server.writeElsewhere('doc-1', SAVED.replace('Absatz zwei.', 'Absatz zwei, dort.'))
    const release = server.hold((request) => request.startsWith('PUT ') && request !== 'PUT doc-1')
    expect(leavingAsks()).toBe(false)

    await passTime(AUTOSAVE_DEBOUNCE_MS)
    expect(desk.handle.conflictCopies).toMatchObject([{ saved: false }])
    expect(leavingAsks()).toBe(true)

    release()
    await settle()
    expect(desk.handle.conflictCopies).toMatchObject([{ saved: true }])
    expect(leavingAsks()).toBe(false)
  })

  it('does not ask without project sync, where the copy stays a document of the project', async () => {
    const opened = createEmptyProjectState()
    const desk = mountDesk({
      initial: {
        ...opened,
        editorDocumentOrder: ['doc-1'],
        editorDocuments: {
          'doc-1': {
            contentMarkdown: SAVED,
            createdAt: '2026-03-04T07:00:00.000Z',
            folderId: null,
            id: 'doc-1',
            revision: 1,
            serverSynced: true,
            source: 'blank',
            syncBase: { contentMarkdown: SAVED, revision: 1 },
            title: 'Plan.md',
            updatedAt: '2026-03-04T07:00:00.000Z',
          },
        },
      },
      syncActive: false,
    })
    await settle()
    desk.type(SAVED.replace('Absatz zwei.', 'Absatz zwei, hier.'))
    const accepted = {
      ...desk.state.editorDocuments['doc-1'],
      contentMarkdown: SAVED.replace('Absatz zwei.', 'Absatz zwei, angenommen.'),
      revision: 2,
    }

    const outcome = await drive(desk.handle.mergeServerDocument('doc-1', accepted))

    expect(outcome.kind).toBe('conflict')
    expect(desk.handle.conflictCopies).toMatchObject([{ saved: false }])
    expect(server.writes).toEqual([])
    expect(leavingAsks()).toBe(false)
  })
})
