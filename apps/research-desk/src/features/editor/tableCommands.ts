import type { Editor } from '@tiptap/react'
import { Fragment, type Node as ProseMirrorNode } from '@tiptap/pm/model'
import { Selection, type EditorState } from '@tiptap/pm/state'

/**
 * Table operations for the interactive live-table controls. Insert/delete/header
 * use the MIT Table extension's built-in commands; move/sort/duplicate/clear
 * rebuild the table node directly (valid for simple tables — we don't offer
 * merged cells). All operations are markdown-safe (structure only; no per-cell
 * color/alignment that GFM can't store).
 */

export type CellContext = {
  tablePos: number
  table: ProseMirrorNode
  rowIndex: number
  colIndex: number
}

/** Resolve the table + the row/column index of the cell containing `pos`. */
export function resolveTableCell(state: EditorState, pos: number): CellContext | null {
  const clamped = Math.min(Math.max(0, pos), state.doc.content.size)
  const $pos = state.doc.resolve(clamped)
  for (let depth = $pos.depth; depth > 0; depth--) {
    if ($pos.node(depth).type.name === 'table') {
      return {
        tablePos: $pos.before(depth),
        table: $pos.node(depth),
        rowIndex: $pos.index(depth),
        colIndex: $pos.index(depth + 1),
      }
    }
  }
  return null
}

function withCaret(editor: Editor, cellPos: number) {
  editor.chain().focus().setTextSelection(cellPos).run()
}

// ---- built-in commands (act on the caret's column/row) ----
export const insertColumnBefore = (editor: Editor, cellPos: number) => {
  withCaret(editor, cellPos)
  editor.chain().focus().addColumnBefore().run()
}
export const insertColumnAfter = (editor: Editor, cellPos: number) => {
  withCaret(editor, cellPos)
  editor.chain().focus().addColumnAfter().run()
}
export const deleteColumnAt = (editor: Editor, cellPos: number) => {
  withCaret(editor, cellPos)
  editor.chain().focus().deleteColumn().run()
}
export const insertRowBefore = (editor: Editor, cellPos: number) => {
  withCaret(editor, cellPos)
  editor.chain().focus().addRowBefore().run()
}
export const insertRowAfter = (editor: Editor, cellPos: number) => {
  withCaret(editor, cellPos)
  editor.chain().focus().addRowAfter().run()
}
export const deleteRowAt = (editor: Editor, cellPos: number) => {
  withCaret(editor, cellPos)
  editor.chain().focus().deleteRow().run()
}
export const toggleHeaderRowAt = (editor: Editor, cellPos: number) => {
  withCaret(editor, cellPos)
  editor.chain().focus().toggleHeaderRow().run()
}

// ---- custom node rebuilds (markdown-safe structural transforms) ----
function cellsOf(row: ProseMirrorNode): ProseMirrorNode[] {
  const cells: ProseMirrorNode[] = []
  row.forEach((cell) => cells.push(cell))
  return cells
}
function rowsOf(table: ProseMirrorNode): ProseMirrorNode[] {
  const rows: ProseMirrorNode[] = []
  table.forEach((row) => rows.push(row))
  return rows
}
function rebuildRow(row: ProseMirrorNode, cells: ProseMirrorNode[]): ProseMirrorNode {
  return row.type.create(row.attrs, Fragment.from(cells))
}
function replaceTable(editor: Editor, ctx: CellContext, rows: ProseMirrorNode[]) {
  const newTable = ctx.table.type.create(ctx.table.attrs, Fragment.from(rows))
  const tr = editor.state.tr.replaceWith(ctx.tablePos, ctx.tablePos + ctx.table.nodeSize, newTable)
  // Keep the caret inside the rebuilt table so the interactive controls stay visible.
  const caret = Math.min(ctx.tablePos + 3, tr.doc.content.size)
  tr.setSelection(Selection.near(tr.doc.resolve(caret)))
  editor.view.dispatch(tr)
  editor.view.focus()
}
function colCount(table: ProseMirrorNode): number {
  return table.childCount > 0 ? cellsOf(table.child(0)).length : 0
}

export const moveColumn = (editor: Editor, cellPos: number, direction: 'left' | 'right') => {
  const ctx = resolveTableCell(editor.state, cellPos)
  if (!ctx) return
  const target = direction === 'left' ? ctx.colIndex - 1 : ctx.colIndex + 1
  if (target < 0 || target >= colCount(ctx.table)) return
  const rows = rowsOf(ctx.table).map((row) => {
    const cells = cellsOf(row)
    const swap = cells[ctx.colIndex]
    cells[ctx.colIndex] = cells[target]
    cells[target] = swap
    return rebuildRow(row, cells)
  })
  replaceTable(editor, ctx, rows)
}

export const duplicateColumn = (editor: Editor, cellPos: number) => {
  const ctx = resolveTableCell(editor.state, cellPos)
  if (!ctx) return
  const rows = rowsOf(ctx.table).map((row) => {
    const cells = cellsOf(row)
    const source = cells[ctx.colIndex]
    cells.splice(ctx.colIndex + 1, 0, source.type.create(source.attrs, source.content))
    return rebuildRow(row, cells)
  })
  replaceTable(editor, ctx, rows)
}

export const clearColumn = (editor: Editor, cellPos: number) => {
  const ctx = resolveTableCell(editor.state, cellPos)
  if (!ctx) return
  const paragraph = editor.schema.nodes.paragraph
  const rows = rowsOf(ctx.table).map((row) => {
    const cells = cellsOf(row)
    const cell = cells[ctx.colIndex]
    cells[ctx.colIndex] = cell.type.create(cell.attrs, Fragment.from(paragraph.create()))
    return rebuildRow(row, cells)
  })
  replaceTable(editor, ctx, rows)
}

export const sortColumn = (editor: Editor, cellPos: number, direction: 'asc' | 'desc') => {
  const ctx = resolveTableCell(editor.state, cellPos)
  if (!ctx) return
  const headerRows: ProseMirrorNode[] = []
  const bodyRows: ProseMirrorNode[] = []
  for (const row of rowsOf(ctx.table)) {
    const first = row.childCount > 0 ? row.child(0) : null
    if (first && first.type.name === 'tableHeader') headerRows.push(row)
    else bodyRows.push(row)
  }
  const sorted = [...bodyRows].sort((a, b) => {
    const ta = a.childCount > ctx.colIndex ? a.child(ctx.colIndex).textContent : ''
    const tb = b.childCount > ctx.colIndex ? b.child(ctx.colIndex).textContent : ''
    return ta.localeCompare(tb, undefined, { numeric: true, sensitivity: 'base' }) * (direction === 'asc' ? 1 : -1)
  })
  replaceTable(editor, ctx, [...headerRows, ...sorted])
}

export const duplicateRow = (editor: Editor, cellPos: number) => {
  const ctx = resolveTableCell(editor.state, cellPos)
  if (!ctx) return
  const rows = rowsOf(ctx.table)
  const source = rows[ctx.rowIndex]
  rows.splice(ctx.rowIndex + 1, 0, source.type.create(source.attrs, source.content))
  replaceTable(editor, ctx, rows)
}

export const moveRow = (editor: Editor, cellPos: number, direction: 'up' | 'down') => {
  const ctx = resolveTableCell(editor.state, cellPos)
  if (!ctx) return
  const target = direction === 'up' ? ctx.rowIndex - 1 : ctx.rowIndex + 1
  const rows = rowsOf(ctx.table)
  if (target < 0 || target >= rows.length) return
  const swap = rows[ctx.rowIndex]
  rows[ctx.rowIndex] = rows[target]
  rows[target] = swap
  replaceTable(editor, ctx, rows)
}
