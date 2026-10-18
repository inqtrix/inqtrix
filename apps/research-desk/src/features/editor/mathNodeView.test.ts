import { createEditorSchemaExtensions } from '@inqtrix/editor-schema'
import { getSchema, type Editor } from '@tiptap/core'
import { EditorState, NodeSelection, type Transaction } from '@tiptap/pm/state'
import { history, undo, redo } from '@tiptap/pm/history'
import { describe, expect, it } from 'vitest'

import { updateMathNodeSource } from './mathNodeView'

const schema = getSchema(createEditorSchemaExtensions())

function fixture(type: 'inlineMath' | 'blockMath') {
  const first = schema.nodes[type].create({ latex: 'a_1' })
  const second = schema.nodes[type].create({ latex: 'b_2' })
  const doc = schema.node('doc', null, type === 'inlineMath'
    ? [schema.node('paragraph', null, [first, second])]
    : [first, second])
  let state = EditorState.create({ schema, doc, plugins: [history()] })
  const positions: number[] = []
  doc.descendants((node, pos) => { if (node.type.name === type) positions.push(pos) })
  let target = positions[1]
  state = state.apply(state.tr.setSelection(NodeSelection.create(state.doc, positions[0])))
  const control = {
    isEditable: true,
    storage: { collaborationReview: { writeMode: 'edit' } },
    get state() { return state },
    view: { dispatch: (transaction: Transaction) => { state = state.apply(transaction) } },
  }
  return {
    editor: control as unknown as Editor,
    control,
    getPos: () => target,
    state: () => state,
    move: () => {
      const prefix = schema.node('paragraph', null, schema.text('prefix'))
      state = state.apply(state.tr.insert(0, prefix))
      target += prefix.nodeSize
    },
    values: () => {
      const values: string[] = []
      state.doc.descendants((node) => { if (node.type.name === type) values.push(node.attrs.latex) })
      return values
    },
  }
}

describe.each(['inlineMath', 'blockMath'] as const)('%s source editing', (type) => {
  it('edits the current node position, independently of the document selection', () => {
    const f = fixture(type)
    expect(updateMathNodeSource(f.editor, f.getPos, type, 'c_3')).toBe(true)
    expect(f.values()).toEqual(['a_1', 'c_3'])
    f.move()
    expect(updateMathNodeSource(f.editor, f.getPos, type, '\\frac{c}{d}')).toBe(true)
    expect(f.values()).toEqual(['a_1', '\\frac{c}{d}'])
  })

  it('keeps attribute edits in ordinary document undo and redo', () => {
    const f = fixture(type)
    updateMathNodeSource(f.editor, f.getPos, type, 'c_3')
    expect(undo(f.state(), f.control.view.dispatch)).toBe(true)
    expect(f.values()).toEqual(['a_1', 'b_2'])
    expect(redo(f.state(), f.control.view.dispatch)).toBe(true)
    expect(f.values()).toEqual(['a_1', 'c_3'])
  })

  it('refuses source changes outside plain edit mode', () => {
    const f = fixture(type)
    for (const mode of ['suggest', 'comment']) {
      f.control.storage.collaborationReview.writeMode = mode
      expect(updateMathNodeSource(f.editor, f.getPos, type, 'c_3')).toBe(false)
    }
    f.control.storage.collaborationReview.writeMode = 'edit'
    f.control.isEditable = false
    expect(updateMathNodeSource(f.editor, f.getPos, type, 'c_3')).toBe(false)
    expect(f.values()).toEqual(['a_1', 'b_2'])
  })

  it('refuses a missing or different node without changing another formula', () => {
    const f = fixture(type)
    expect(updateMathNodeSource(f.editor, () => undefined, type, 'c_3')).toBe(false)
    expect(updateMathNodeSource(f.editor, f.getPos, type === 'inlineMath' ? 'blockMath' : 'inlineMath', 'c_3')).toBe(false)
    expect(f.values()).toEqual(['a_1', 'b_2'])
  })
})
