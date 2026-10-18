import { useEffect, useState } from 'react'
import type { Editor } from '@tiptap/react'
import type { EditorState } from '@tiptap/pm/state'
import {
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  ChevronUp,
  Copy,
  Eraser,
  MoreHorizontal,
  MoreVertical,
  Plus,
  Table2,
  Trash2,
} from '@/components/icons'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import {
  clearColumn,
  deleteColumnAt,
  deleteRowAt,
  duplicateColumn,
  duplicateRow,
  insertColumnAfter,
  insertColumnBefore,
  insertRowAfter,
  insertRowBefore,
  moveColumn,
  moveRow,
  resolveTableCell,
  sortColumn,
  toggleHeaderRowAt,
} from './tableCommands'

export type TableControlsLabels = {
  columnOptions: string
  rowOptions: string
  addColumn: string
  addRow: string
  colInsertLeft: string
  colInsertRight: string
  colMoveLeft: string
  colMoveRight: string
  sortAsc: string
  sortDesc: string
  colDuplicate: string
  colClear: string
  toggleHeaderRow: string
  colDelete: string
  rowInsertAbove: string
  rowInsertBelow: string
  rowMoveUp: string
  rowMoveDown: string
  rowDuplicate: string
  rowDelete: string
}

type Geom = {
  tablePos: number
  table: { left: number; top: number; right: number; bottom: number; width: number; height: number }
  cols: { index: number; left: number; width: number }[]
  rows: { index: number; top: number; height: number }[]
}

/** Position inside the cell at [rowIndex][colIndex] of the table at `tablePos`
 * (simple tables only — no merged cells). Used to anchor a command's caret. */
function cellPosFor(state: EditorState, tablePos: number, rowIndex: number, colIndex: number): number | null {
  const table = state.doc.nodeAt(tablePos)
  if (!table || table.type.name !== 'table' || rowIndex >= table.childCount) return null
  let pos = tablePos + 1
  for (let r = 0; r < rowIndex; r += 1) pos += table.child(r).nodeSize
  const row = table.child(rowIndex)
  if (colIndex >= row.childCount) return null
  let cellStart = pos + 1
  for (let c = 0; c < colIndex; c += 1) cellStart += row.child(c).nodeSize
  return cellStart + 1
}

function tableElementAtCaret(editor: Editor): HTMLElement | null {
  try {
    const dom = editor.view.domAtPos(editor.state.selection.from)
    const node = dom.node
    const element = node instanceof Element ? node : node.parentElement
    return (element?.closest('table') as HTMLElement | null) ?? null
  } catch {
    return null
  }
}

/**
 * Interactive table overlay (live mode): per-column and per-row "…" menus plus
 * edge "+" buttons, shown while the caret is in a table. Operations are
 * markdown-safe (`tableCommands.ts`); no per-cell color/merge. The overlay only
 * measures geometry and dispatches commands — it never touches the table's
 * `.editor-prose` rendering, so the table looks unchanged.
 */
export function TableControls({ editor, labels }: { editor: Editor; labels: TableControlsLabels }) {
  const [geom, setGeom] = useState<Geom | null>(null)

  useEffect(() => {
    let raf = 0
    const measure = () => {
      const ctx = resolveTableCell(editor.state, editor.state.selection.from)
      if (!ctx || !editor.isEditable) {
        setGeom(null)
        return
      }
      const tableEl = tableElementAtCaret(editor)
      const firstRow = tableEl?.querySelector('tr')
      if (!tableEl || !firstRow) {
        setGeom(null)
        return
      }
      const tableRect = tableEl.getBoundingClientRect()
      const cols = Array.from(firstRow.children).map((cell, index) => {
        const rect = (cell as HTMLElement).getBoundingClientRect()
        return { index, left: rect.left, width: rect.width }
      })
      const rows = Array.from(tableEl.querySelectorAll('tr')).map((row, index) => {
        const rect = (row as HTMLElement).getBoundingClientRect()
        return { index, top: rect.top, height: rect.height }
      })
      setGeom({
        tablePos: ctx.tablePos,
        table: {
          left: tableRect.left,
          top: tableRect.top,
          right: tableRect.right,
          bottom: tableRect.bottom,
          width: tableRect.width,
          height: tableRect.height,
        },
        cols,
        rows,
      })
    }
    const schedule = () => {
      cancelAnimationFrame(raf)
      raf = requestAnimationFrame(measure)
    }
    editor.on('transaction', schedule)
    editor.on('selectionUpdate', schedule)
    window.addEventListener('resize', schedule)
    window.addEventListener('scroll', schedule, true)
    schedule()
    return () => {
      editor.off('transaction', schedule)
      editor.off('selectionUpdate', schedule)
      window.removeEventListener('resize', schedule)
      window.removeEventListener('scroll', schedule, true)
      cancelAnimationFrame(raf)
    }
  }, [editor])

  if (!geom) return null

  const colCellPos = (colIndex: number) => cellPosFor(editor.state, geom.tablePos, 0, colIndex)
  const rowCellPos = (rowIndex: number) => cellPosFor(editor.state, geom.tablePos, rowIndex, 0)
  const runCol = (colIndex: number, op: (editor: Editor, pos: number) => void) => {
    const pos = colCellPos(colIndex)
    if (pos != null) op(editor, pos)
  }
  const runRow = (rowIndex: number, op: (editor: Editor, pos: number) => void) => {
    const pos = rowCellPos(rowIndex)
    if (pos != null) op(editor, pos)
  }

  const triggerClass =
    'pointer-events-auto grid place-items-center rounded-[4px] border border-border bg-surface text-muted-foreground shadow-[0_1px_2px_var(--shadow-hairline)] transition-colors hover:bg-accent hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring'
  const lastCol = geom.cols.length - 1
  const lastRow = geom.rows.length - 1

  return (
    <div className="pointer-events-none fixed inset-0 z-40">
      {/* Column "…" triggers above each column */}
      {geom.cols.map((col) => (
        <DropdownMenu key={`col-${col.index}`}>
          <DropdownMenuTrigger asChild>
            <button
              aria-label={labels.columnOptions}
              className={`${triggerClass} h-[14px] w-6`}
              style={{ position: 'fixed', left: col.left + col.width / 2 - 12, top: geom.table.top - 20 }}
              type="button"
            >
              <MoreHorizontal className="size-3.5" />
            </button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="start" className="w-56">
            <DropdownMenuItem onSelect={() => runCol(col.index, insertColumnBefore)}>
              <ChevronLeft className="size-4 text-muted-foreground" />
              {labels.colInsertLeft}
            </DropdownMenuItem>
            <DropdownMenuItem onSelect={() => runCol(col.index, insertColumnAfter)}>
              <ChevronRight className="size-4 text-muted-foreground" />
              {labels.colInsertRight}
            </DropdownMenuItem>
            <DropdownMenuSeparator />
            <DropdownMenuItem onSelect={() => runCol(col.index, (e, p) => moveColumn(e, p, 'left'))}>
              <ChevronLeft className="size-4 text-muted-foreground" />
              {labels.colMoveLeft}
            </DropdownMenuItem>
            <DropdownMenuItem onSelect={() => runCol(col.index, (e, p) => moveColumn(e, p, 'right'))}>
              <ChevronRight className="size-4 text-muted-foreground" />
              {labels.colMoveRight}
            </DropdownMenuItem>
            <DropdownMenuItem onSelect={() => runCol(col.index, (e, p) => sortColumn(e, p, 'asc'))}>
              <ChevronDown className="size-4 text-muted-foreground" />
              {labels.sortAsc}
            </DropdownMenuItem>
            <DropdownMenuItem onSelect={() => runCol(col.index, (e, p) => sortColumn(e, p, 'desc'))}>
              <ChevronUp className="size-4 text-muted-foreground" />
              {labels.sortDesc}
            </DropdownMenuItem>
            <DropdownMenuSeparator />
            <DropdownMenuItem onSelect={() => runCol(col.index, duplicateColumn)}>
              <Copy className="size-4 text-muted-foreground" />
              {labels.colDuplicate}
            </DropdownMenuItem>
            <DropdownMenuItem onSelect={() => runCol(col.index, clearColumn)}>
              <Eraser className="size-4 text-muted-foreground" />
              {labels.colClear}
            </DropdownMenuItem>
            <DropdownMenuItem onSelect={() => runCol(col.index, toggleHeaderRowAt)}>
              <Table2 className="size-4 text-muted-foreground" />
              {labels.toggleHeaderRow}
            </DropdownMenuItem>
            <DropdownMenuSeparator />
            <DropdownMenuItem
              className="text-destructive focus:bg-destructive/10 focus:text-destructive"
              onSelect={() => runCol(col.index, deleteColumnAt)}
            >
              <Trash2 className="size-4" />
              {labels.colDelete}
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      ))}

      {/* Row "…" triggers left of each row */}
      {geom.rows.map((row) => (
        <DropdownMenu key={`row-${row.index}`}>
          <DropdownMenuTrigger asChild>
            <button
              aria-label={labels.rowOptions}
              className={`${triggerClass} h-6 w-[14px]`}
              style={{ position: 'fixed', left: geom.table.left - 20, top: row.top + row.height / 2 - 12 }}
              type="button"
            >
              <MoreVertical className="size-3.5" />
            </button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="start" className="w-52">
            <DropdownMenuItem onSelect={() => runRow(row.index, insertRowBefore)}>
              <ChevronUp className="size-4 text-muted-foreground" />
              {labels.rowInsertAbove}
            </DropdownMenuItem>
            <DropdownMenuItem onSelect={() => runRow(row.index, insertRowAfter)}>
              <ChevronDown className="size-4 text-muted-foreground" />
              {labels.rowInsertBelow}
            </DropdownMenuItem>
            <DropdownMenuSeparator />
            <DropdownMenuItem onSelect={() => runRow(row.index, (e, p) => moveRow(e, p, 'up'))}>
              <ChevronUp className="size-4 text-muted-foreground" />
              {labels.rowMoveUp}
            </DropdownMenuItem>
            <DropdownMenuItem onSelect={() => runRow(row.index, (e, p) => moveRow(e, p, 'down'))}>
              <ChevronDown className="size-4 text-muted-foreground" />
              {labels.rowMoveDown}
            </DropdownMenuItem>
            <DropdownMenuItem onSelect={() => runRow(row.index, duplicateRow)}>
              <Copy className="size-4 text-muted-foreground" />
              {labels.rowDuplicate}
            </DropdownMenuItem>
            <DropdownMenuSeparator />
            <DropdownMenuItem
              className="text-destructive focus:bg-destructive/10 focus:text-destructive"
              onSelect={() => runRow(row.index, deleteRowAt)}
            >
              <Trash2 className="size-4" />
              {labels.rowDelete}
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      ))}

      {/* "+" add column (right edge) / add row (bottom edge) */}
      <button
        aria-label={labels.addColumn}
        className={`${triggerClass} h-6 w-5`}
        onClick={() => runCol(lastCol, insertColumnAfter)}
        style={{ position: 'fixed', left: geom.table.right + 4, top: geom.table.top + geom.table.height / 2 - 12 }}
        type="button"
      >
        <Plus className="size-3.5" />
      </button>
      <button
        aria-label={labels.addRow}
        className={`${triggerClass} h-5 w-6`}
        onClick={() => runRow(lastRow, insertRowAfter)}
        style={{ position: 'fixed', left: geom.table.left + geom.table.width / 2 - 12, top: geom.table.bottom + 4 }}
        type="button"
      >
        <Plus className="size-3.5" />
      </button>
    </div>
  )
}
