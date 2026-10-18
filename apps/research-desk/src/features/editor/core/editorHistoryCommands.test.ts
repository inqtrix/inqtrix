import type { Editor } from '@tiptap/react'
import { describe, expect, it, vi } from 'vitest'

import {
  canRunEditorHistoryCommand,
  runEditorHistoryCommand,
} from './editorHistoryCommands'

function editorWithoutHistory(): Editor {
  return {
    can: () => ({}),
    chain: () => ({
      focus: () => ({}),
    }),
    isDestroyed: false,
    isEditable: true,
  } as unknown as Editor
}

describe('editor history command guards', () => {
  it('preserves source-input focus when the caller requests it', () => {
    const run = vi.fn(() => true)
    const undo = vi.fn(() => ({ run }))
    const focus = vi.fn(() => ({ undo }))
    const editor = {
      chain: () => ({ focus, undo }), isDestroyed: false, isEditable: true,
    } as unknown as Editor
    expect(runEditorHistoryCommand(editor, 'undo', { focus: false })).toBe(true)
    expect(focus).not.toHaveBeenCalled()
    expect(undo).toHaveBeenCalledOnce()
    expect(run).toHaveBeenCalledOnce()
  })

  it('keeps collaboration toolbars inert while history is not installed', () => {
    const editor = editorWithoutHistory()

    expect(canRunEditorHistoryCommand(editor, 'undo')).toBe(false)
    expect(canRunEditorHistoryCommand(editor, 'redo')).toBe(false)
    expect(runEditorHistoryCommand(editor, 'undo')).toBe(false)
    expect(runEditorHistoryCommand(editor, 'redo')).toBe(false)
  })

  it('runs an available history command through the focused chain', () => {
    const run = vi.fn(() => true)
    const undo = vi.fn(() => ({ run }))
    const focus = vi.fn(() => ({ undo }))
    const canUndo = vi.fn(() => true)
    const editor = {
      can: () => ({ undo: canUndo }),
      chain: () => ({ focus }),
      isDestroyed: false,
      isEditable: true,
    } as unknown as Editor

    expect(canRunEditorHistoryCommand(editor, 'undo')).toBe(true)
    expect(runEditorHistoryCommand(editor, 'undo')).toBe(true)
    expect(canUndo).toHaveBeenCalledOnce()
    expect(focus).toHaveBeenCalledOnce()
    expect(undo).toHaveBeenCalledOnce()
    expect(run).toHaveBeenCalledOnce()
  })

  it('disables history commands while the editor is read-only', () => {
    // A live collaboration editor keeps its Yjs undo history while it is
    // read-only (reconnecting); running it would write to the document.
    const run = vi.fn(() => true)
    const history = vi.fn(() => ({ run }))
    const focus = vi.fn(() => ({ redo: history, undo: history }))
    const canRun = vi.fn(() => true)
    const editor = {
      can: () => ({ redo: canRun, undo: canRun }),
      chain: () => ({ focus, redo: history, undo: history }),
      isDestroyed: false,
      isEditable: false,
    } as unknown as Editor

    expect(canRunEditorHistoryCommand(editor, 'undo')).toBe(false)
    expect(canRunEditorHistoryCommand(editor, 'redo')).toBe(false)
    expect(runEditorHistoryCommand(editor, 'undo')).toBe(false)
    expect(runEditorHistoryCommand(editor, 'redo', { focus: false })).toBe(false)
    expect(canRun).not.toHaveBeenCalled()
    expect(history).not.toHaveBeenCalled()
    expect(run).not.toHaveBeenCalled()
  })
})
