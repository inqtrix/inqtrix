import { getSchema } from '@tiptap/core'
import type { Node as ProseMirrorNode, Schema } from '@tiptap/pm/model'
import { EditorState } from '@tiptap/pm/state'
import { describe, expect, it, vi } from 'vitest'
import {
  createEditorSchemaExtensions,
  projectFinalJson,
  projectOriginalJson,
  suggestionDescriptors,
  transformToInqtrixSuggestionTransaction,
  UnsupportedSuggestionStructureError,
} from '../src/index'

type NestedBlockCase = {
  document: ProseMirrorNode
  insertedChild: ProseMirrorNode
  parentType: string
}

function paragraph(schema: Schema, text: string): ProseMirrorNode {
  return schema.node('paragraph', null, schema.text(text))
}

function nestedBlockCases(schema: Schema, childCount: 1 | 2): NestedBlockCase[] {
  const childTexts = Array.from(
    { length: childCount },
    (_, index) => (index === 0 ? 'First' : 'Second'),
  )
  const paragraphs = childTexts.map((text) => paragraph(schema, text))
  const listItems = childTexts.map((text) => (
    schema.node('listItem', null, [paragraph(schema, text)])
  ))
  const taskItems = childTexts.map((text) => (
    schema.node('taskItem', { checked: false }, [paragraph(schema, text)])
  ))
  return [
    {
      document: schema.node('doc', null, [schema.node('blockquote', null, paragraphs)]),
      insertedChild: paragraph(schema, 'Inserted'),
      parentType: 'blockquote',
    },
    {
      document: schema.node('doc', null, [
        schema.node('bulletList', null, [schema.node('listItem', null, paragraphs)]),
      ]),
      insertedChild: paragraph(schema, 'Inserted'),
      parentType: 'listItem',
    },
    {
      document: schema.node('doc', null, [
        schema.node('taskList', null, [
          schema.node('taskItem', { checked: false }, paragraphs),
        ]),
      ]),
      insertedChild: paragraph(schema, 'Inserted'),
      parentType: 'taskItem',
    },
    ...(['tableCell', 'tableHeader'] as const).map((parentType) => ({
      document: schema.node('doc', null, [
        schema.node('table', null, [
          schema.node('tableRow', null, [schema.node(parentType, null, paragraphs)]),
        ]),
      ]),
      insertedChild: paragraph(schema, 'Inserted'),
      parentType,
    })),
    {
      document: schema.node('doc', null, [schema.node('bulletList', null, listItems)]),
      insertedChild: schema.node('listItem', null, [paragraph(schema, 'Inserted')]),
      parentType: 'bulletList',
    },
    {
      document: schema.node('doc', null, [schema.node('orderedList', null, listItems)]),
      insertedChild: schema.node('listItem', null, [paragraph(schema, 'Inserted')]),
      parentType: 'orderedList',
    },
    {
      document: schema.node('doc', null, [schema.node('taskList', null, taskItems)]),
      insertedChild: schema.node('taskItem', { checked: false }, [paragraph(schema, 'Inserted')]),
      parentType: 'taskList',
    },
  ]
}

function findChildBoundary(
  document: ProseMirrorNode,
  parentType: string,
  childIndex: number,
): { from: number; to: number } {
  let boundary: { from: number; to: number } | null = null
  document.descendants((node, position) => {
    if (node.type.name !== parentType) return true
    const child = node.child(childIndex)
    let from = position + 1
    for (let index = 0; index < childIndex; index += 1) from += node.child(index).nodeSize
    boundary = { from, to: from + child.nodeSize }
    return false
  })
  if (!boundary) throw new Error(`Missing nested parent ${parentType}`)
  return boundary
}

describe('nested block suggestions', () => {
  it('rejects insertions in every block-bearing nested parent', () => {
    const schema = getSchema(createEditorSchemaExtensions({ enableUndoRedo: false }))
    expect(schema.nodes.blockquote?.spec.marks).not.toBe('insertion modification deletion')

    for (const { document, insertedChild, parentType } of nestedBlockCases(schema, 1)) {
      const state = EditorState.create({ schema, doc: document })
      const { to } = findChildBoundary(document, parentType, 0)
      const direct = state.tr.insert(to, insertedChild)
      expect(() => transformToInqtrixSuggestionTransaction(
        direct,
        state,
        { authorId: 'user-nested', createdAt: 10, patchId: `patch-${parentType}` },
        () => `suggestion-${parentType}`,
      ), parentType).toThrow(UnsupportedSuggestionStructureError)
    }
  })

  it('rejects deletions in every block-bearing nested parent', () => {
    const schema = getSchema(createEditorSchemaExtensions({ enableUndoRedo: false }))

    for (const { document, parentType } of nestedBlockCases(schema, 2)) {
      const state = EditorState.create({ schema, doc: document })
      const { from, to } = findChildBoundary(document, parentType, 1)
      const direct = state.tr.delete(from, to)
      expect(() => transformToInqtrixSuggestionTransaction(
        direct,
        state,
        { authorId: 'user-nested', createdAt: 11, patchId: `patch-${parentType}` },
        () => `suggestion-${parentType}`,
      ), parentType).toThrow(UnsupportedSuggestionStructureError)
    }
  })

  it('tracks code-block text while preserving code-only formatting semantics', () => {
    const schema = getSchema(createEditorSchemaExtensions({ enableUndoRedo: false }))
    const document = schema.node('doc', null, [schema.node('codeBlock', null, schema.text('First'))])
    const state = EditorState.create({ schema, doc: document })
    const direct = state.tr.insertText('!', 6)
    const expectedFinal = state.apply(direct).doc.toJSON()
    const tracked = state.apply(transformToInqtrixSuggestionTransaction(
      direct,
      state,
      { authorId: 'user-code', createdAt: 12, patchId: 'patch-code' },
      () => 'suggestion-code',
    )).doc

    expect(schema.nodes.codeBlock?.spec.marks).toBe('insertion modification deletion')
    expect(projectOriginalJson(tracked)).toEqual(document.toJSON())
    expect(projectFinalJson(tracked)).toEqual(expectedFinal)
  })

  it('rejects existing-table topology changes before the upstream transform', () => {
    const schema = getSchema(createEditorSchemaExtensions({ enableUndoRedo: false }))
    const row = schema.node('tableRow', null, [
      schema.node('tableCell', null, [paragraph(schema, 'Cell')]),
    ])
    const document = schema.node('doc', null, [schema.node('table', null, [row])])
    const state = EditorState.create({ schema, doc: document })
    const { to } = findChildBoundary(document, 'table', 0)
    const structuralChange = state.tr.insert(to, row)
    const generateSuggestionId = vi.fn(() => 'suggestion-table')

    expect(() => transformToInqtrixSuggestionTransaction(
      structuralChange,
      state,
      { authorId: 'user-table', createdAt: 13, patchId: 'patch-table' },
      generateSuggestionId,
    )).toThrow(UnsupportedSuggestionStructureError)
    expect(generateSuggestionId).not.toHaveBeenCalled()
  })

  it('rejects whole-table insertion as an unsupported block suggestion', () => {
    const schema = getSchema(createEditorSchemaExtensions({ enableUndoRedo: false }))
    const document = schema.node('doc', null, [paragraph(schema, 'Before')])
    const table = schema.node('table', null, [
      schema.node('tableRow', null, [
        schema.node('tableCell', null, [paragraph(schema, 'Cell')]),
      ]),
    ])
    const state = EditorState.create({ schema, doc: document })
    const direct = state.tr.insert(state.doc.content.size, table)
    expect(() => transformToInqtrixSuggestionTransaction(
      direct,
      state,
      { authorId: 'user-table', createdAt: 14, patchId: 'patch-whole-table' },
      () => 'suggestion-whole-table',
    )).toThrow(UnsupportedSuggestionStructureError)
  })
})
