import { history, undo, undoDepth } from '@codemirror/commands'
import { EditorState } from '@codemirror/state'
import { describe, expect, it } from 'vitest'

import { hostTextChange, hostTextTransaction } from './sourceHostText'

function show(state: EditorState, next: string): EditorState {
  const spec = hostTextTransaction(state, next)
  if (!spec) throw new Error('expected a change')
  return state.update(spec).state
}

describe("the source view takes another writer's text", () => {
  it('replaces only the range that differs', () => {
    // "zwei" and "drei" share their end: only "zw" becomes "dr".
    expect(hostTextChange('Absatz eins.\n\nAbsatz zwei.', 'Absatz eins.\n\nAbsatz drei.'))
      .toEqual({ from: 21, insert: 'dr', to: 23 })
    expect(hostTextChange('gleich', 'gleich')).toBeNull()
    // A repeated stretch never makes the ends cross.
    expect(hostTextChange('aaa', 'aa')).toEqual({ from: 2, insert: '', to: 3 })
  })

  it('keeps the caret on its character when the change lies before it', () => {
    const state = EditorState.create({ doc: 'eins zwei drei', selection: { anchor: 10 } })
    const shown = show(state, 'eins und zwei drei')

    expect(shown.doc.toString()).toBe('eins und zwei drei')
    expect(shown.selection.main.anchor).toBe(14)
    expect(shown.doc.sliceString(shown.selection.main.anchor)).toBe('drei')
  })

  it("leaves the other writer's change out of the undo history", () => {
    let state = EditorState.create({ doc: 'eins zwei', extensions: [history()] })
    state = state.update({ changes: { from: 9, insert: ' drei' }, userEvent: 'input.type' }).state
    expect(undoDepth(state)).toBe(1)

    state = show(state, 'Eins und zwei drei')
    expect(undoDepth(state)).toBe(1)

    let undone = state
    undo({ dispatch: (transaction) => { undone = transaction.state }, state })
    // Undo takes back the own edit only; the other writer's change stays.
    expect(undone.doc.toString()).toBe('Eins und zwei')
  })
})
