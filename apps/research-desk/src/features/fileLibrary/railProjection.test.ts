import { describe, expect, it } from 'vitest'
import { indexBackedCollectionIds, railVisibleServerCollections } from './helpers'

describe('rail projection of indexes and server collections', () => {
  const indexes = [
    { serverCollectionId: 'kc_built' },
    { serverCollectionId: null },
    { serverCollectionId: undefined },
  ]

  it('collects the collections that merely back a local index', () => {
    expect([...indexBackedCollectionIds(indexes)]).toEqual(['kc_built'])
    expect(indexBackedCollectionIds([]).size).toBe(0)
  })

  it('hides an index-backed collection from the server-collection list', () => {
    const collections = [{ id: 'kc_built' }, { id: 'kc_shared' }, { id: 'kc_standalone' }]
    // A built index stays THE entity in the rail; its storage collection must
    // not show up a second time next to it.
    expect(
      railVisibleServerCollections(collections, indexBackedCollectionIds(indexes)).map((c) => c.id),
    ).toEqual(['kc_shared', 'kc_standalone'])
  })

  it('keeps every collection while no index is built yet', () => {
    const collections = [{ id: 'kc_shared' }]
    expect(
      railVisibleServerCollections(collections, indexBackedCollectionIds([{ serverCollectionId: null }])),
    ).toEqual(collections)
  })
})
