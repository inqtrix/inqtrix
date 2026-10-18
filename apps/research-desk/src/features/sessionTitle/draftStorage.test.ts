import { afterEach, beforeEach, expect, it } from 'vitest'
import { clearTitleDrafts, readTitleDraft, updateTitleDraftIdentity, writeTitleDraft } from './draftStorage'

const original = Object.getOwnPropertyDescriptor(globalThis, 'sessionStorage')
let entries: Map<string, string>
beforeEach(() => {
  entries = new Map()
  Object.defineProperty(globalThis, 'sessionStorage', { configurable: true, value: {
    get length() { return entries.size }, key: (index: number) => [...entries.keys()][index] ?? null,
    getItem: (key: string) => entries.get(key) ?? null,
    setItem: (key: string, value: string) => entries.set(key, value),
    removeItem: (key: string) => entries.delete(key),
  } })
})
afterEach(() => {
  if (original) Object.defineProperty(globalThis, 'sessionStorage', original)
  else delete (globalThis as { sessionStorage?: Storage }).sessionStorage
})

it('retains an unresolved rename for reload while separating accounts, desks and records', () => {
  writeTitleDraft('account:workspace', 'chat', 'one', { title: 'My project', revision: 3 })
  expect(readTitleDraft('account:workspace', 'chat', 'one')).toEqual({ title: 'My project', revision: 3 })
  expect(readTitleDraft('other:workspace', 'chat', 'one')).toBeNull()
  expect(readTitleDraft('account:workspace', 'agent', 'one')).toBeNull()
  expect(readTitleDraft('account:workspace', 'chat', 'two')).toBeNull()
  writeTitleDraft('account:workspace', 'chat', 'one', null)
  expect(readTitleDraft('account:workspace', 'chat', 'one')).toBeNull()
})

it('survives initial auth discovery and clears only on a confirmed identity transition', () => {
  writeTitleDraft('a:workspace', 'agent', 'one', { title: 'Private draft', revision: 0 })
  let identity = updateTitleDraftIdentity(undefined, null, true)
  identity = updateTitleDraftIdentity(identity, null, false)
  identity = updateTitleDraftIdentity(identity, 'a', true)
  expect(readTitleDraft('a:workspace', 'agent', 'one')?.title).toBe('Private draft')
  expect(updateTitleDraftIdentity(identity, 'b', true)).toBe('b')
  expect(readTitleDraft('a:workspace', 'agent', 'one')).toBeNull()
})

it('rejects corrupt drafts and tolerates unavailable storage', () => {
  writeTitleDraft('a', 'chat', 'one', { title: 'Draft', revision: 0 })
  entries.set([...entries.keys()][0], '{broken')
  expect(readTitleDraft('a', 'chat', 'one')).toBeNull()
  Object.defineProperty(globalThis, 'sessionStorage', { configurable: true, get() { throw new Error('blocked') } })
  expect(() => writeTitleDraft('a', 'chat', 'one', { title: 'Draft', revision: 0 })).not.toThrow()
  expect(readTitleDraft('a', 'chat', 'one')).toBeNull()
  expect(() => clearTitleDrafts()).not.toThrow()
})
