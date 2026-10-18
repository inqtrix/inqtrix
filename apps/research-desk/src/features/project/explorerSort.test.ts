import { describe, expect, it } from 'vitest'

import {
  adoptVisibleExplorerOrder,
  defaultExplorerSortState,
  orderPinnedExplorerItems,
  resolveExplorerSortState,
  sortExplorerFolders,
  sortExplorerItems,
} from './explorerSort'

type Row = { id: string; time: string; title: string }

const rows: Row[] = [
  { id: 'a', time: '2026-08-20T10:00:00.000Z', title: 'Alpha' },
  { id: 'b', time: '2026-08-29T09:00:00.000Z', title: 'zeta' },
  { id: 'c', time: '2026-08-25T12:00:00.000Z', title: 'Ärger' },
  { id: 'd', time: '2026-08-25T12:00:00.000Z', title: 'Bericht 10' },
  { id: 'e', time: '2026-08-25T12:00:00.000Z', title: 'Bericht 2' },
]

const timeOf = (row: Row) => row.time
const titleOf = (row: Row) => row.title
const idOf = (row: Row) => row.id

describe('sortExplorerItems', () => {
  it('recent puts newest activity first and breaks time ties by title', () => {
    expect(sortExplorerItems(rows, 'recent', timeOf, titleOf).map(idOf))
      .toEqual(['b', 'c', 'e', 'd', 'a'])
  })

  it('name sorts locale-aware with numeric collation', () => {
    expect(sortExplorerItems(rows, 'name', timeOf, titleOf).map(idOf))
      .toEqual(['a', 'c', 'e', 'd', 'b'])
  })

  it('manual returns the given order untouched', () => {
    expect(sortExplorerItems(rows, 'manual', timeOf, titleOf).map(idOf))
      .toEqual(['a', 'b', 'c', 'd', 'e'])
  })

  it('unparseable timestamps sink to the end in recent mode', () => {
    const withBroken: Row[] = [
      { id: 'x', time: 'not-a-date', title: 'X' },
      ...rows.slice(0, 2),
    ]
    const sorted = sortExplorerItems(withBroken, 'recent', timeOf, titleOf)
    expect(sorted[sorted.length - 1]?.id).toBe('x')
  })
})

describe('sortExplorerFolders', () => {
  const folders = [
    { id: 'f2', title: 'Projekte 10' },
    { id: 'f1', title: 'Ältere' },
    { id: 'f3', title: 'projekte 2' },
  ]

  it('is alphabetical in both automatic modes and untouched in manual', () => {
    for (const mode of ['recent', 'name'] as const) {
      expect(sortExplorerFolders(folders, mode, (f) => f.title).map((f) => f.id))
        .toEqual(['f1', 'f3', 'f2'])
    }
    expect(sortExplorerFolders(folders, 'manual', (f) => f.title).map((f) => f.id))
      .toEqual(['f2', 'f1', 'f3'])
  })
})

describe('orderPinnedExplorerItems', () => {
  it('automatic modes render pin order (first pinned on top)', () => {
    expect(
      orderPinnedExplorerItems(rows, ['d', 'b', 'a'], 'recent', idOf).map(idOf),
    ).toEqual(['d', 'b', 'a', 'c', 'e'])
  })

  it('manual keeps the master-list order', () => {
    expect(
      orderPinnedExplorerItems(rows, ['d', 'b', 'a'], 'manual', idOf).map(idOf),
    ).toEqual(['a', 'b', 'c', 'd', 'e'])
  })

  it('rows missing from the pin array keep their relative position at the end', () => {
    expect(
      orderPinnedExplorerItems(rows, ['c'], 'recent', idOf).map(idOf),
    ).toEqual(['c', 'a', 'b', 'd', 'e'])
  })
})

describe('adoptVisibleExplorerOrder', () => {
  it('adopts the visible sequence and appends uncovered ids behind', () => {
    expect(adoptVisibleExplorerOrder(['a', 'b', 'c', 'd'], ['c', 'a']))
      .toEqual(['c', 'a', 'b', 'd'])
  })

  it('drops visible ids the order does not know', () => {
    expect(adoptVisibleExplorerOrder(['a', 'b'], ['ghost', 'b', 'a']))
      .toEqual(['b', 'a'])
  })
})

describe('resolveExplorerSortState', () => {
  it('accepts valid modes and defaults unknowns per desk', () => {
    expect(resolveExplorerSortState({ chat: 'manual', editor: 'bogus' }))
      .toEqual({ ...defaultExplorerSortState(), chat: 'manual' })
    expect(resolveExplorerSortState(undefined))
      .toEqual(defaultExplorerSortState())
  })
})
