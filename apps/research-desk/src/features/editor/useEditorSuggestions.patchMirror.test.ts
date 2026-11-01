import { beforeEach, describe, expect, it, vi } from 'vitest'

import { listEditorDocumentPatches } from '@/api/inqtrixClient'
import { createEmptyProjectState } from '@/features/project/seedProject'
import type { EditorDocumentRecord } from '@/features/project/types'
import { useEditorSuggestions, type UseEditorSuggestionsArgs } from './useEditorSuggestions'

// The hook runs for real on a stand-in for React's render cycle: an effect
// runs when it is new or when one of its dependencies changed, as in a commit.
// No DOM in this lane, and a source pin could not prove the rerun.
const cycle = vi.hoisted(() => ({
  committed: [] as Array<{ cleanup?: unknown; deps?: readonly unknown[] }>,
  rendered: [] as Array<{ deps?: readonly unknown[]; effect: () => unknown }>,
}))

vi.mock('react', async (original) => ({
  ...await original<typeof import('react')>(),
  useEffect: (effect: () => unknown, deps?: readonly unknown[]) => {
    cycle.rendered.push({ deps, effect })
  },
  useMemo: <T,>(factory: () => T) => factory(),
  useRef: <T,>(value: T) => ({ current: value }),
  useState: <T,>(value: T | (() => T)) => [
    typeof value === 'function' ? (value as () => T)() : value,
    () => undefined,
  ],
}))
vi.mock('@/api/inqtrixClient', async (original) => ({
  ...await original<typeof import('@/api/inqtrixClient')>(),
  listEditorDocumentPatches: vi.fn(),
}))

const mountedEditor = {} as UseEditorSuggestionsArgs['activeEditor']

function render(args: Partial<UseEditorSuggestionsArgs>): void {
  cycle.rendered = []
  useEditorSuggestions({
    activeDocument: null,
    activeEditor: mountedEditor,
    attachedCommentIds: [],
    attachedRefs: [],
    capabilities: null,
    collaboration: {} as UseEditorSuggestionsArgs['collaboration'],
    comments: [],
    dispatch: () => undefined,
    handedOverDiscussions: [],
    locale: 'de',
    onGlobalSuccess: () => undefined,
    selectedModelTier: null,
    state: createEmptyProjectState(),
    ...args,
  })
  cycle.rendered.forEach((entry, index) => {
    const previous = cycle.committed[index]
    const changed = !previous
      || !entry.deps
      || !previous.deps
      || entry.deps.some((value, position) => !Object.is(value, previous.deps?.[position]))
    if (!changed) {
      cycle.committed[index] = previous
      return
    }
    if (typeof previous?.cleanup === 'function') previous.cleanup()
    cycle.committed[index] = { cleanup: entry.effect(), deps: entry.deps }
  })
}

function document(overrides: Partial<EditorDocumentRecord>): EditorDocumentRecord {
  return {
    contentMarkdown: 'Text',
    createdAt: '2026-10-30T08:00:00.000Z',
    folderId: null,
    id: 'document-new',
    revision: 0,
    source: 'blank',
    title: 'Neu.md',
    updatedAt: '2026-10-30T08:00:00.000Z',
    ...overrides,
  }
}

const patchRequests = () => vi.mocked(listEditorDocumentPatches).mock.calls.map((call) => call[0])

beforeEach(() => {
  cycle.committed = []
  cycle.rendered = []
  vi.mocked(listEditorDocumentPatches).mockReset().mockResolvedValue({ data: [] } as never)
})

describe('pending agent suggestions of the open document', () => {
  it('asks nothing while the document has not reached the server', () => {
    // The first autosave had not written the row yet: the server answered 404
    // on every new document, and the console showed it.
    render({ activeDocument: document({}) })
    render({ activeDocument: document({ serverSynced: false }) })
    expect(patchRequests()).toEqual([])
  })

  it('asks once as soon as the document is on the server', () => {
    const draft = document({})
    render({ activeDocument: draft })
    expect(patchRequests()).toEqual([])
    const saved = { ...draft, revision: 1, serverSynced: true }
    render({ activeDocument: saved })
    expect(patchRequests()).toEqual(['document-new'])
    // A later render of the same document asks no second time.
    render({ activeDocument: { ...saved, revision: 2 } })
    expect(patchRequests()).toEqual(['document-new'])
  })

  it('asks when a document that came from the server is opened', () => {
    render({ activeDocument: document({ id: 'document-loaded', serverSynced: true }) })
    expect(patchRequests()).toEqual(['document-loaded'])
  })

  it('leaves collaboration documents and demo desks alone', () => {
    render({ activeDocument: document({ contentMode: 'collaboration', serverSynced: true }) })
    render({
      activeDocument: document({ id: 'document-demo', serverSynced: true }),
      state: { ...createEmptyProjectState(), connection: { kind: 'demo', writable: false } },
    })
    expect(patchRequests()).toEqual([])
  })
})
