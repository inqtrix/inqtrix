import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import {
  clearCollaborationCommentDrafts,
  clearOtherGuestsCommentDrafts,
  collaborationCommentDraftDocumentKey,
} from './useCollaborationComments'

const PREFIX = 'inqtrix:collaboration-comment-drafts:v1'
const GUEST_A = '77777777-7777-4777-8777-777777777777'
const GUEST_B = '66666666-6666-4666-8666-666666666666'

/** The node-only suite has no browser storage; a Map stands in for it. */
function fakeStorage(): Storage {
  const values = new Map<string, string>()
  return {
    get length() {
      return values.size
    },
    clear: () => values.clear(),
    getItem: (key: string) => values.get(key) ?? null,
    key: (index: number) => [...values.keys()][index] ?? null,
    removeItem: (key: string) => {
      values.delete(key)
    },
    setItem: (key: string, value: string) => {
      values.set(key, value)
    },
  } as Storage
}

const original = Object.getOwnPropertyDescriptor(globalThis, 'localStorage')

function installStorage(storage: Storage | undefined): void {
  Object.defineProperty(globalThis, 'localStorage', {
    configurable: true,
    value: storage,
    writable: true,
  })
}

function storedKeys(): string[] {
  const store = globalThis.localStorage
  return Array.from({ length: store.length }, (_, index) => store.key(index) ?? '').sort()
}

beforeEach(() => {
  installStorage(fakeStorage())
})

afterEach(() => {
  if (original) Object.defineProperty(globalThis, 'localStorage', original)
  else delete (globalThis as { localStorage?: Storage }).localStorage
})

describe('unsent comment drafts of guests', () => {
  it('keys a guest\'s drafts by the guest, so another guest of the document never reads them', () => {
    const first = collaborationCommentDraftDocumentKey({ documentId: 'doc-1', generation: 2, guestId: GUEST_A })
    const next = collaborationCommentDraftDocumentKey({ documentId: 'doc-1', generation: 2, guestId: GUEST_B })

    expect(first).toBe(`guest:${GUEST_A}:doc-1:g2`)
    expect(next).toBe(`guest:${GUEST_B}:doc-1:g2`)
    // A signed-in user's drafts keep their workspace key.
    expect(collaborationCommentDraftDocumentKey({ documentId: 'doc-1', generation: 2, workspaceId: 'workspace-1' }))
      .toBe('workspace-1:doc-1:g2')
  })

  it('deletes the drafts of the guest whose access ended, and only those', () => {
    localStorage.setItem(`${PREFIX}:guest:${GUEST_A}:doc-1:g2`, '{"new":"A"}')
    localStorage.setItem(`${PREFIX}:guest:${GUEST_B}:doc-1:g2`, '{"new":"B"}')

    clearCollaborationCommentDrafts({ documentId: 'doc-1', generation: 2, guestId: GUEST_A })

    expect(storedKeys()).toEqual([`${PREFIX}:guest:${GUEST_B}:doc-1:g2`])
  })

  it('deletes every other guest\'s drafts once a guest session holds this browser', () => {
    // One guest session per browser: whoever held it before can never
    // return here, so their unsent drafts belong to an access that ended.
    localStorage.setItem(`${PREFIX}:guest:${GUEST_A}:doc-1:g2`, '{"new":"A"}')
    localStorage.setItem(`${PREFIX}:guest:${GUEST_A}:doc-2:g1`, '{"t1":"A2"}')
    localStorage.setItem(`${PREFIX}:guest:doc-1:g2`, '{"new":"unkeyed"}')
    localStorage.setItem(`${PREFIX}:guest:${GUEST_B}:doc-1:g2`, '{"new":"B"}')
    localStorage.setItem(`${PREFIX}:workspace-1:doc-1:g2`, '{"new":"member"}')
    localStorage.setItem('inqtrix.agent.plan-draft:run-1:v1', '{}')

    clearOtherGuestsCommentDrafts(GUEST_B)

    expect(storedKeys()).toEqual([
      'inqtrix.agent.plan-draft:run-1:v1',
      `${PREFIX}:guest:${GUEST_B}:doc-1:g2`,
      `${PREFIX}:workspace-1:doc-1:g2`,
    ])
  })

  it('treats storage that refuses to work as holding no drafts', () => {
    installStorage({
      get length(): number {
        throw new Error('blocked')
      },
      key: () => {
        throw new Error('blocked')
      },
      removeItem: () => {
        throw new Error('blocked')
      },
    } as unknown as Storage)

    expect(() => clearOtherGuestsCommentDrafts(GUEST_B)).not.toThrow()
    expect(() => clearCollaborationCommentDrafts({ documentId: 'doc-1', generation: 2, guestId: GUEST_A }))
      .not.toThrow()
  })
})
