import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { markdown } from '@codemirror/lang-markdown'
import { HighlightStyle, syntaxHighlighting } from '@codemirror/language'
import { Compartment } from '@codemirror/state'
import { EditorView } from '@codemirror/view'
import { basicSetup } from 'codemirror'
import { tags } from '@lezer/highlight'
import {
  AlignCenter,
  AlignLeft,
  AlignRight,
  Plus,
  Table2,
  Trash2,
  WrapText,
  X,
  type LucideIcon,
} from '@/components/icons'
import { Button } from '@/components/ui/button'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { cn } from '@/lib/utils'
import {
  createMarkdownTable,
  deleteMarkdownTableColumn,
  deleteMarkdownTableRow,
  findMarkdownTableAtOffset,
  formatMarkdownTables,
  insertMarkdownTableColumn,
  insertMarkdownTableRow,
  serializeMarkdownTable,
  setMarkdownTableAlignment,
  updateMarkdownTableCell,
  type MarkdownTableModel,
  type MarkdownTableSelection,
} from './markdownSourceFormatting'

type MarkdownSourceEditorLabels = {
  addColumn: string
  addRow: string
  closeTableEditor: string
  columnLabel: string
  deleteColumn: string
  deleteRow: string
  editor: string
  formatTables: string
  insertOrEditTable: string
  lineWrap: string
  tableAlignmentCenter: string
  tableAlignmentLeft: string
  tableAlignmentRight: string
  tableColumn: string
  tableEditor: string
  tableLines: string
  tableRows: string
}

type MarkdownSourceEditorProps = {
  labels: MarkdownSourceEditorLabels
  onChange: (value: string) => void
  value: string
}

const markdownSourceTheme = EditorView.theme({
  '&': {
    backgroundColor: 'var(--background)',
    color: 'var(--foreground)',
    fontSize: '0.8125rem',
    height: '100%',
  },
  '&.cm-focused': {
    outline: 'none',
  },
  '.cm-activeLine': {
    backgroundColor: 'color-mix(in oklab, var(--surface) 58%, transparent)',
  },
  '.cm-activeLineGutter': {
    backgroundColor: 'color-mix(in oklab, var(--surface) 65%, transparent)',
    color: 'var(--foreground)',
  },
  '.cm-content': {
    caretColor: 'var(--brand)',
    minHeight: '100%',
    padding: '1.65rem 2.25rem 4rem 0.5rem',
  },
  '.cm-cursor': {
    borderLeftColor: 'var(--brand)',
  },
  '.cm-gutters .cm-foldGutter': {
    display: 'none !important',
  },
  '.cm-gutters': {
    backgroundColor: 'var(--background)',
    borderRight: '1px solid var(--border)',
    color: 'color-mix(in oklab, var(--muted-foreground) 68%, transparent)',
    fontSize: '0.75rem',
    paddingLeft: '0.5rem',
  },
  '.cm-gutterElement': {
    lineHeight: '1.55rem',
  },
  '.cm-lineNumbers .cm-gutterElement': {
    fontFeatureSettings: '"tnum"',
    fontSize: '0.75rem',
    fontWeight: '500',
    minWidth: '2.1rem',
    padding: '0 0.625rem 0 0.25rem',
    textAlign: 'right',
  },
  '.cm-line': {
    lineHeight: '1.55rem',
    padding: '0 0.25rem 0 0.75rem',
  },
  '.cm-matchingBracket, .cm-nonmatchingBracket': {
    backgroundColor: 'var(--brand-subtle)',
    color: 'var(--foreground)',
  },
  '.cm-panels': {
    backgroundColor: 'var(--card)',
    color: 'var(--foreground)',
  },
  '.cm-panels input': {
    backgroundColor: 'var(--background)',
    border: '1px solid var(--border)',
    borderRadius: '0.375rem',
    color: 'var(--foreground)',
  },
  '.cm-scroller': {
    fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace',
    lineHeight: '1.55rem',
  },
  '.cm-searchMatch': {
    backgroundColor: 'color-mix(in oklab, var(--warning) 30%, transparent)',
  },
  '.cm-searchMatch-selected': {
    backgroundColor: 'color-mix(in oklab, var(--brand) 24%, transparent)',
  },
  '.cm-selectionBackground, .cm-content ::selection': {
    backgroundColor: 'color-mix(in oklab, var(--brand) 24%, transparent) !important',
  },
})

const markdownHighlightStyle = HighlightStyle.define([
  { tag: tags.heading, color: 'var(--foreground)', fontWeight: '700' },
  { tag: tags.contentSeparator, color: 'var(--muted-foreground)' },
  { tag: tags.link, color: 'var(--brand)', textDecoration: 'underline', textUnderlineOffset: '0.18em' },
  { tag: tags.url, color: 'var(--brand)' },
  { tag: tags.emphasis, fontStyle: 'italic' },
  { tag: tags.strong, color: 'var(--foreground)', fontWeight: '700' },
  { tag: tags.monospace, color: 'var(--brand)' },
  { tag: tags.quote, color: 'var(--muted-foreground)' },
  { tag: tags.list, color: 'var(--brand)', fontWeight: '650' },
  { tag: tags.punctuation, color: 'var(--muted-foreground)' },
])

export function MarkdownSourceEditor({ labels, onChange, value }: MarkdownSourceEditorProps) {
  const containerRef = useRef<HTMLDivElement | null>(null)
  const editorRef = useRef<EditorView | null>(null)
  const initialValueRef = useRef(value)
  const onChangeRef = useRef(onChange)
  const activeTableRef = useRef<MarkdownTableSelection | null>(null)
  const lastActiveTableRangeRef = useRef<string | null>(null)
  const isApplyingExternalValueRef = useRef(false)
  const lineWrapCompartment = useMemo(() => new Compartment(), [])
  const [activeTable, setActiveTable] = useState<MarkdownTableSelection | null>(null)
  const [isTableEditorOpen, setIsTableEditorOpen] = useState(false)
  const [isLineWrapEnabled, setIsLineWrapEnabled] = useState(true)

  useEffect(() => {
    onChangeRef.current = onChange
  }, [onChange])

  const updateActiveTable = useCallback((editor: EditorView) => {
    const nextTable = findMarkdownTableAtOffset(
      editor.state.doc.toString(),
      editor.state.selection.main.anchor,
    )
    activeTableRef.current = nextTable
    setActiveTable(nextTable)

    if (!nextTable) {
      lastActiveTableRangeRef.current = null
      setIsTableEditorOpen(false)
      return
    }

    const rangeKey = `${nextTable.from}:${nextTable.to}`
    if (lastActiveTableRangeRef.current !== rangeKey) {
      lastActiveTableRangeRef.current = rangeKey
      setIsTableEditorOpen(true)
    }
  }, [])

  useEffect(() => {
    const parent = containerRef.current
    if (!parent) return

    const editor = new EditorView({
      doc: initialValueRef.current,
      extensions: [
        basicSetup,
        markdown(),
        markdownSourceTheme,
        syntaxHighlighting(markdownHighlightStyle),
        lineWrapCompartment.of(EditorView.lineWrapping),
        EditorView.contentAttributes.of({
          'aria-label': labels.editor,
          spellcheck: 'false',
        }),
        EditorView.updateListener.of((update) => {
          if (update.docChanged && !isApplyingExternalValueRef.current) {
            onChangeRef.current(update.state.doc.toString())
          }
          if (update.docChanged || update.selectionSet) {
            updateActiveTable(update.view)
          }
        }),
      ],
      parent,
    })

    editorRef.current = editor
    updateActiveTable(editor)
    return () => {
      editor.destroy()
      editorRef.current = null
    }
  }, [labels.editor, lineWrapCompartment, updateActiveTable])

  useEffect(() => {
    const editor = editorRef.current
    if (!editor) return
    const currentValue = editor.state.doc.toString()
    if (currentValue === value) return

    isApplyingExternalValueRef.current = true
    editor.dispatch({
      changes: { from: 0, insert: value, to: currentValue.length },
      selection: { anchor: Math.min(editor.state.selection.main.anchor, value.length) },
    })
    isApplyingExternalValueRef.current = false
    updateActiveTable(editor)
  }, [updateActiveTable, value])

  useEffect(() => {
    const editor = editorRef.current
    if (!editor) return
    editor.dispatch({
      effects: lineWrapCompartment.reconfigure(isLineWrapEnabled ? EditorView.lineWrapping : []),
    })
  }, [isLineWrapEnabled, lineWrapCompartment])

  const formatTables = useCallback(() => {
    const editor = editorRef.current
    if (!editor) return
    const currentValue = editor.state.doc.toString()
    const formattedValue = formatMarkdownTables(currentValue)
    if (formattedValue === currentValue) return

    editor.dispatch({
      changes: { from: 0, insert: formattedValue, to: currentValue.length },
      effects: editor.scrollSnapshot(),
      selection: { anchor: Math.min(editor.state.selection.main.anchor, formattedValue.length) },
    })
  }, [])

  const applyTableChange = useCallback((table: MarkdownTableModel) => {
    const editor = editorRef.current
    const activeSelection = activeTableRef.current
    if (!editor || !activeSelection) return

    const currentValue = editor.state.doc.toString()
    const latestSelection = findMarkdownTableAtOffset(currentValue, activeSelection.from)
    if (!latestSelection) return

    const nextTableMarkdown = serializeMarkdownTable(table)
    editor.dispatch({
      changes: {
        from: latestSelection.from,
        insert: nextTableMarkdown,
        to: latestSelection.to,
      },
      effects: editor.scrollSnapshot(),
      selection: { anchor: latestSelection.from },
    })
  }, [])

  const insertOrEditTable = useCallback(() => {
    const editor = editorRef.current
    if (!editor) return

    if (activeTableRef.current) {
      setIsTableEditorOpen(true)
      return
    }

    const selection = editor.state.selection.main
    const currentValue = editor.state.doc.toString()
    const previousChar = selection.from > 0 ? currentValue[selection.from - 1] : '\n'
    const nextChar = selection.to < currentValue.length ? currentValue[selection.to] : '\n'
    const prefix = previousChar === '\n' ? '' : '\n\n'
    const suffix = nextChar === '\n' ? '' : '\n\n'
    const tableMarkdown = serializeMarkdownTable(createMarkdownTable(labels.columnLabel))
    const insert = `${prefix}${tableMarkdown}${suffix}`
    const tableStart = selection.from + prefix.length

    editor.dispatch({
      changes: { from: selection.from, insert, to: selection.to },
      effects: editor.scrollSnapshot(),
      selection: { anchor: tableStart },
    })
    setIsTableEditorOpen(true)
  }, [labels.columnLabel])

  return (
    <div className="markdown-source-editor flex min-h-0 flex-1 flex-col bg-background">
      <div className="flex shrink-0 items-center justify-between border-b border-border bg-background/95 px-4 py-2">
        <div className="flex items-center gap-2 t-meta-sm font-semibold uppercase tracking-wide text-muted-foreground">
          <CodeLabel />
          {labels.editor}
        </div>
        <div className="flex items-center gap-1">
          <SourceToolbarButton
            active={isLineWrapEnabled}
            icon={WrapText}
            label={labels.lineWrap}
            onClick={() => setIsLineWrapEnabled((current) => !current)}
          />
          <SourceToolbarButton
            active={Boolean(activeTable && isTableEditorOpen)}
            icon={Table2}
            label={labels.insertOrEditTable}
            onClick={insertOrEditTable}
          />
        </div>
      </div>
      {activeTable && isTableEditorOpen ? (
        <MarkdownTableEditor
          labels={labels}
          onChange={applyTableChange}
          onClose={() => setIsTableEditorOpen(false)}
          onFormat={formatTables}
          selection={activeTable}
        />
      ) : null}
      <div className="min-h-0 flex-1" ref={containerRef} />
    </div>
  )
}

function CodeLabel() {
  return <span className="h-1.5 w-1.5 rounded-full bg-brand" aria-hidden="true" />
}

function SourceToolbarButton({
  active,
  icon: Icon,
  label,
  onClick,
}: {
  active?: boolean
  icon: LucideIcon
  label: string
  onClick: () => void
}) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button
          aria-label={label}
          aria-pressed={active}
          className={cn('size-7 rounded-md', active && 'bg-brand-subtle text-brand')}
          onClick={onClick}
          size="icon"
          type="button"
          variant="ghost"
        >
          <Icon className="size-4" />
        </Button>
      </TooltipTrigger>
      <TooltipContent>{label}</TooltipContent>
    </Tooltip>
  )
}

function MarkdownTableEditor({
  labels,
  onChange,
  onClose,
  onFormat,
  selection,
}: {
  labels: MarkdownSourceEditorLabels
  onChange: (table: MarkdownTableModel) => void
  onClose: () => void
  onFormat: () => void
  selection: MarkdownTableSelection
}) {
  const table = selection.table
  const columnCount = table.alignments.length
  const bodyRowCount = Math.max(0, table.rows.length - 1)
  const [activeColumnIndex, setActiveColumnIndex] = useState(0)
  const activeColumnAlignment = table.alignments[activeColumnIndex] ?? 'none'

  useEffect(() => {
    setActiveColumnIndex((current) => Math.min(current, Math.max(0, columnCount - 1)))
  }, [columnCount])

  return (
    <section className="shrink-0 border-b border-border bg-surface/35 px-4 py-3">
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <div className="flex min-w-0 items-center gap-2">
          <div className="flex size-7 shrink-0 items-center justify-center rounded-md border border-border bg-background text-muted-foreground">
            <Table2 className="size-3.5" />
          </div>
          <div className="min-w-0">
            <div className="text-xs font-semibold text-foreground">{labels.tableEditor}</div>
            <div className="truncate t-meta-sm text-muted-foreground">
              {columnCount} {labels.tableColumn} · {bodyRowCount} {labels.tableRows} · {labels.tableLines}{' '}
              {selection.fromLine}-{selection.toLine}
            </div>
          </div>
        </div>
        <div className="flex items-center gap-1">
          <SourceActionButton icon={Plus} label={labels.addRow} onClick={() => onChange(
            insertMarkdownTableRow(table, table.rows.length - 1),
          )} />
          <SourceActionButton icon={Plus} label={labels.addColumn} onClick={() => {
            setActiveColumnIndex(columnCount)
            onChange(insertMarkdownTableColumn(table, columnCount - 1, `${labels.columnLabel} ${columnCount + 1}`))
          }} />
          <div className="mx-1 h-5 w-px bg-border" aria-hidden="true" />
          <TableAlignmentButton
            active={activeColumnAlignment === 'left'}
            icon={AlignLeft}
            label={labels.tableAlignmentLeft}
            onClick={() => onChange(setMarkdownTableAlignment(
              table,
              activeColumnIndex,
              activeColumnAlignment === 'left' ? 'none' : 'left',
            ))}
          />
          <TableAlignmentButton
            active={activeColumnAlignment === 'center'}
            icon={AlignCenter}
            label={labels.tableAlignmentCenter}
            onClick={() => onChange(setMarkdownTableAlignment(
              table,
              activeColumnIndex,
              activeColumnAlignment === 'center' ? 'none' : 'center',
            ))}
          />
          <TableAlignmentButton
            active={activeColumnAlignment === 'right'}
            icon={AlignRight}
            label={labels.tableAlignmentRight}
            onClick={() => onChange(setMarkdownTableAlignment(
              table,
              activeColumnIndex,
              activeColumnAlignment === 'right' ? 'none' : 'right',
            ))}
          />
          <SourceActionButton
            disabled={columnCount <= 2}
            icon={Trash2}
            label={labels.deleteColumn}
            onClick={() => {
              setActiveColumnIndex((current) => Math.max(0, current - 1))
              onChange(deleteMarkdownTableColumn(table, activeColumnIndex))
            }}
          />
          <div className="mx-1 h-5 w-px bg-border" aria-hidden="true" />
          <SourceActionButton icon={Table2} label={labels.formatTables} onClick={onFormat} />
          <SourceActionButton icon={X} label={labels.closeTableEditor} onClick={onClose} />
        </div>
      </div>
      <div className="overflow-x-auto rounded-md border border-border bg-background shadow-[0_1px_0_color-mix(in_oklab,var(--border)_70%,transparent)]">
        <table className="w-full table-fixed border-collapse text-left t-meta">
          <thead>
            <tr className="border-b border-border bg-surface/70">
              {table.rows[0]?.map((cell, columnIndex) => (
                <th
                  className={cn(
                    'min-w-0 border-r border-border p-0 align-top last:border-r-0',
                    activeColumnIndex === columnIndex && 'bg-brand-subtle/35',
                  )}
                  key={`header-${columnIndex}`}
                  scope="col"
                >
                  <div className="border-b border-border/70 px-2 py-1.5">
                    <span className="truncate t-meta-sm font-medium uppercase tracking-wide text-muted-foreground">
                      {labels.columnLabel} {columnIndex + 1}
                    </span>
                  </div>
                  <MarkdownTableCellTextarea
                    ariaLabel={`${labels.columnLabel} ${columnIndex + 1}`}
                    isHeader
                    onChange={(nextValue) => onChange(updateMarkdownTableCell(table, 0, columnIndex, nextValue))}
                    onFocus={() => setActiveColumnIndex(columnIndex)}
                    value={cell}
                  />
                </th>
              ))}
              <th className="w-9 bg-surface/70" aria-hidden="true" />
            </tr>
          </thead>
          <tbody>
            {table.rows.slice(1).map((row, bodyIndex) => {
              const rowIndex = bodyIndex + 1
              return (
                <tr className="border-b border-border last:border-b-0" key={`row-${rowIndex}`}>
                  {row.map((cell, columnIndex) => (
                    <td className="min-w-0 border-r border-border p-0 align-top last:border-r-0" key={`cell-${rowIndex}-${columnIndex}`}>
                      <MarkdownTableCellTextarea
                        ariaLabel={`${labels.tableRows} ${rowIndex}, ${labels.columnLabel} ${columnIndex + 1}`}
                        onChange={(nextValue) => onChange(updateMarkdownTableCell(
                          table,
                          rowIndex,
                          columnIndex,
                          nextValue,
                        ))}
                        onFocus={() => setActiveColumnIndex(columnIndex)}
                        value={cell}
                      />
                    </td>
                  ))}
                  <td className="w-9 bg-surface/35 px-1 text-center align-middle">
                    <SourceActionButton
                      disabled={table.rows.length <= 2}
                      icon={Trash2}
                      label={labels.deleteRow}
                      onClick={() => onChange(deleteMarkdownTableRow(table, rowIndex))}
                    />
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </section>
  )
}

function MarkdownTableCellTextarea({
  ariaLabel,
  isHeader,
  onChange,
  onFocus,
  value,
}: {
  ariaLabel: string
  isHeader?: boolean
  onChange: (value: string) => void
  onFocus?: () => void
  value: string
}) {
  const textareaRef = useRef<HTMLTextAreaElement | null>(null)

  const resize = useCallback(() => {
    const textarea = textareaRef.current
    if (!textarea) return
    textarea.style.height = 'auto'
    textarea.style.height = `${Math.max(34, textarea.scrollHeight)}px`
  }, [])

  useLayoutEffect(() => {
    resize()
  }, [resize, value])

  return (
    <textarea
      aria-label={ariaLabel}
      className={cn(
        'block min-h-[2.125rem] w-full resize-none overflow-hidden bg-transparent px-2 py-1.5 t-meta leading-5 text-foreground outline-none transition-colors placeholder:text-muted-foreground/70 focus:bg-brand-subtle/60',
        isHeader && 'font-semibold',
      )}
      onChange={(event) => onChange(event.target.value)}
      onFocus={onFocus}
      onInput={resize}
      ref={textareaRef}
      rows={1}
      spellCheck={false}
      value={value}
    />
  )
}

function SourceActionButton({
  disabled,
  icon: Icon,
  label,
  onClick,
}: {
  disabled?: boolean
  icon: LucideIcon
  label: string
  onClick: () => void
}) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button
          aria-label={label}
          className="size-7 rounded-md"
          disabled={disabled}
          onClick={onClick}
          size="icon"
          type="button"
          variant="ghost"
        >
          <Icon className="size-3.5" />
        </Button>
      </TooltipTrigger>
      <TooltipContent>{label}</TooltipContent>
    </Tooltip>
  )
}

function TableAlignmentButton({
  active,
  disabled,
  icon: Icon,
  label,
  onClick,
}: {
  active?: boolean
  disabled?: boolean
  icon: LucideIcon
  label: string
  onClick: () => void
}) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button
          aria-label={label}
          aria-pressed={active}
          className={cn('size-6 rounded-sm text-muted-foreground', active && 'bg-brand-subtle text-brand')}
          disabled={disabled}
          onClick={onClick}
          size="icon"
          type="button"
          variant="ghost"
        >
          <Icon className="size-3" />
        </Button>
      </TooltipTrigger>
      <TooltipContent>{label}</TooltipContent>
    </Tooltip>
  )
}
