import type { Editor } from '@tiptap/core'
import { INQTRIX_STRUCTURE_COMMAND_META } from '@inqtrix/editor-schema'
import { describe, expect, it, vi } from 'vitest'
import { runBlockAction, type BlockActionId } from './blockActions'
import { slashItemsForEditor } from './slashCommand'
import {
  buildSlashItems,
  filterSlashItems,
  type SlashLabels,
} from './slashItems'

const labels: SlashLabels = {
  blockquote: 'Zitat',
  bulletList: 'Aufzählung',
  closeHint: 'Schließen',
  codeBlock: 'Codeblock',
  divider: 'Trennlinie',
  empty: 'Keine Treffer',
  groupInsert: 'Einfügen',
  groupStyle: 'Stil',
  heading1: 'Überschrift 1',
  heading2: 'Überschrift 2',
  heading3: 'Überschrift 3',
  navHint: 'Navigieren',
  orderedList: 'Nummerierte Liste',
  selectHint: 'Auswählen',
  suggestUnavailable: 'Nur im Modus Bearbeiten verfügbar',
  table: 'Tabelle',
  taskList: 'Aufgabenliste',
  text: 'Absatz',
  title: 'Blöcke',
}

const expectedIds: BlockActionId[] = [
  'paragraph',
  'heading1',
  'heading2',
  'heading3',
  'bulletList',
  'orderedList',
  'taskList',
  'blockquote',
  'codeBlock',
  'table',
  'divider',
]

function editorWithMode(writeMode: 'edit' | 'suggest'): Editor {
  return {
    storage: { collaborationReview: { writeMode } },
  } as unknown as Editor
}

describe('slash command matrix', () => {
  it('exposes the complete eleven-action matrix in its stable display order', () => {
    expect(buildSlashItems(labels).map((item) => item.id)).toEqual(expectedIds)
  })

  it('keeps all actions available in edit mode', () => {
    const items = slashItemsForEditor(
      buildSlashItems(labels),
      editorWithMode('edit'),
      labels,
    )

    expect(items).toHaveLength(11)
    expect(items.every((item) => item.disabled === false)).toBe(true)
    expect(items.every((item) => item.description === undefined)).toBe(true)
  })

  it('disables only table and divider in suggest mode with an actionable reason', () => {
    const items = slashItemsForEditor(
      buildSlashItems(labels),
      editorWithMode('suggest'),
      labels,
    )

    expect(items.filter((item) => item.disabled).map((item) => item.id)).toEqual([
      'table',
      'divider',
    ])
    expect(items.filter((item) => item.disabled).map((item) => item.description)).toEqual([
      labels.suggestUnavailable,
      labels.suggestUnavailable,
    ])
    expect(items.filter((item) => !item.disabled)).toHaveLength(9)
  })

  it.each([
    ['h1', 'heading1'],
    ['todo', 'taskList'],
    ['tabelle', 'table'],
    ['horizontal', 'divider'],
    ['nummeriert', 'orderedList'],
  ])('finds %s through localized and stable synonyms', (query, expectedId) => {
    expect(filterSlashItems(buildSlashItems(labels), query).map((item) => item.id))
      .toContain(expectedId)
  })
})

describe('runBlockAction', () => {
  it.each(expectedIds)('executes %s, removes the slash query, and records review metadata', (id) => {
    const calls: Array<{ name: string; args: unknown[] }> = []
    const transaction = {
      setMeta: vi.fn(),
    }
    const chain = {
      command: vi.fn((callback: (props: { tr: typeof transaction }) => boolean) => {
        calls.push({ name: 'command', args: [] })
        expect(callback({ tr: transaction })).toBe(true)
        return chain
      }),
      deleteRange: vi.fn((...args: unknown[]) => {
        calls.push({ name: 'deleteRange', args })
        return chain
      }),
      focus: vi.fn(() => {
        calls.push({ name: 'focus', args: [] })
        return chain
      }),
      insertTable: vi.fn((...args: unknown[]) => {
        calls.push({ name: 'insertTable', args })
        return chain
      }),
      run: vi.fn(() => true),
      setHorizontalRule: vi.fn((...args: unknown[]) => {
        calls.push({ name: 'setHorizontalRule', args })
        return chain
      }),
      setParagraph: vi.fn((...args: unknown[]) => {
        calls.push({ name: 'setParagraph', args })
        return chain
      }),
      toggleBlockquote: vi.fn((...args: unknown[]) => {
        calls.push({ name: 'toggleBlockquote', args })
        return chain
      }),
      toggleBulletList: vi.fn((...args: unknown[]) => {
        calls.push({ name: 'toggleBulletList', args })
        return chain
      }),
      toggleCodeBlock: vi.fn((...args: unknown[]) => {
        calls.push({ name: 'toggleCodeBlock', args })
        return chain
      }),
      toggleHeading: vi.fn((...args: unknown[]) => {
        calls.push({ name: 'toggleHeading', args })
        return chain
      }),
      toggleOrderedList: vi.fn((...args: unknown[]) => {
        calls.push({ name: 'toggleOrderedList', args })
        return chain
      }),
      toggleTaskList: vi.fn((...args: unknown[]) => {
        calls.push({ name: 'toggleTaskList', args })
        return chain
      }),
    }
    const editor = {
      chain: () => chain,
    } as unknown as Editor

    expect(runBlockAction(editor, id, { from: 1, to: 4 })).toBe(true)
    expect(chain.deleteRange).toHaveBeenCalledWith({ from: 1, to: 4 })
    expect(transaction.setMeta).toHaveBeenCalledWith(
      INQTRIX_STRUCTURE_COMMAND_META,
      { action: id, commandRange: { from: 1, to: 4 } },
    )
    expect(chain.run).toHaveBeenCalledOnce()

    const actionCalls = calls.filter(({ name }) => !['focus', 'command', 'deleteRange'].includes(name))
    expect(actionCalls).toHaveLength(1)
  })
})
