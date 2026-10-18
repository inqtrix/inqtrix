import {
  getExtensionField,
  type Extensions,
  type JSONContent,
  type MarkdownRendererHelpers,
  type MarkdownToken,
  type RenderContext,
} from '@tiptap/core'
import Document from '@tiptap/extension-document'
import Highlight from '@tiptap/extension-highlight'
import Link from '@tiptap/extension-link'
import { BlockMath, InlineMath } from '@tiptap/extension-mathematics'
import { Table, TableCell, TableHeader, TableRow } from '@tiptap/extension-table'
import TaskItem from '@tiptap/extension-task-item'
import TaskList from '@tiptap/extension-task-list'
import TextAlign from '@tiptap/extension-text-align'
import { Markdown } from '@tiptap/markdown'
import StarterKit from '@tiptap/starter-kit'
import { EDITOR_SUGGESTION_BLOCK_PARENTS } from './constants.js'
import {
  SuggestionDeletion,
  SuggestionInsertion,
  SuggestionModification,
} from './suggestionMarks.js'
import { StructureSuggestionAttributes } from './structureSuggestions.js'

export type EditorSchemaExtensionOptions = {
  enableUndoRedo?: boolean
  resizableTables?: boolean
}

/**
 * The text alignments the editor offers, and the only values the text
 * alignment attribute may hold. The value is rendered into a style
 * attribute, so any other text would be style injection: `left; display:
 * none` would hide a block from every reader. The schema enforces the list
 * for every writer, so neither a direct edit nor a suggestion can carry
 * anything else.
 */
export const EDITOR_TEXT_ALIGNMENTS = Object.freeze(['left', 'center', 'right', 'justify'] as const)

export type EditorTextAlignment = typeof EDITOR_TEXT_ALIGNMENTS[number]

export function isEditorTextAlignment(value: unknown): value is EditorTextAlignment | null {
  return value === null || (
    typeof value === 'string'
    && (EDITOR_TEXT_ALIGNMENTS as readonly string[]).includes(value)
  )
}

const OfferedTextAlign = TextAlign.extend({
  addGlobalAttributes() {
    return (this.parent?.() ?? []).map((entry) => ({
      ...entry,
      attributes: Object.fromEntries(Object.entries(entry.attributes).map(([name, attribute]) => [
        name,
        name === 'textAlign'
          ? {
              ...attribute,
              validate: (value: unknown) => {
                if (!isEditorTextAlignment(value)) {
                  throw new RangeError('Text alignment must be one of the offered alignments')
                }
              },
            }
          : attribute,
      ])),
    }))
  },
})

/** The spellings of a line break the Markdown reader takes inside a table
 * cell: `<br>`, `<br/>` and `<br />`, nothing else. */
const TABLE_CELL_LINE_BREAK = /^<br(?: ?\/)?>$/

const readTableMarkdown = getExtensionField<NonNullable<typeof Table.config.parseMarkdown>>(
  Table,
  'parseMarkdown',
)

/**
 * The table with one read rule of this package. A table cell is one line of
 * Markdown, so the serializer writes a line break inside a cell (a hard
 * break, or the break between two paragraphs of the cell) as `<br>`. The
 * Markdown reader interprets inline HTML only where a DOM exists; without
 * one, as in the collaboration service, `<br>` would come back as the text
 * `<br>`. Inside a table cell the reader therefore takes exactly the
 * spellings in `TABLE_CELL_LINE_BREAK` as the hard break the serializer
 * wrote. Every other piece of HTML keeps the reader's own handling, a code
 * span keeps its text, and text a user typed never reaches this rule as HTML:
 * the serializer escapes its angle brackets.
 */
const EditorTable = Table.extend({
  parseMarkdown: (token, helpers) => readTableMarkdown(withTableCellLineBreaks(token), helpers),
})

function withTableCellLineBreaks(token: MarkdownToken): MarkdownToken {
  const cell = (entry: MarkdownToken): MarkdownToken => (
    entry.tokens ? { ...entry, tokens: lineBreaksAsHardBreaks(entry.tokens) } : entry
  )
  return {
    ...token,
    header: Array.isArray(token.header) ? token.header.map(cell) : token.header,
    rows: Array.isArray(token.rows)
      ? token.rows.map((row: MarkdownToken[]) => row.map(cell))
      : token.rows,
  }
}

/** *tokens* with each line-break tag as the token of a hard break, also
 * inside formatting and link text. */
function lineBreaksAsHardBreaks(tokens: MarkdownToken[]): MarkdownToken[] {
  return tokens.map((token) => {
    const raw = token.raw
    if (token.type === 'html' && raw !== undefined && TABLE_CELL_LINE_BREAK.test(raw)) {
      return { type: 'br', raw }
    }
    return token.tokens ? { ...token, tokens: lineBreaksAsHardBreaks(token.tokens) } : token
  })
}

const SUGGESTION_MARKS = 'insertion modification deletion'

const SUGGESTION_STARTER_BLOCK_PARENTS: ReadonlySet<string> = new Set(
  EDITOR_SUGGESTION_BLOCK_PARENTS.filter((name) => (
    name === 'codeBlock'
  )),
)

// A line of paragraph text that begins like a Markdown block: an ATX heading,
// a bullet or ordered list item, or a line of only `-` or `=` (a thematic
// break, or a setext underline after the line before it). The serializer
// escapes `*`, `_`, `>`, `<` and code fences itself.
const LEADING_BLOCK_MARKER = /^(?:#{1,6}(?=[ \t]|$)|[-+](?=[ \t]|$)|-+(?=[ \t]*$)|=+(?=[ \t]*$))|^([0-9]{1,9})([.)])(?=[ \t]|$)/gm

/**
 * Paragraph Markdown with the block marker escaped at the start of the
 * paragraph and of every line after a hard break, as CommonMark escapes it,
 * so text a writer typed ("## Der Bereich", "1. Bundesliga") reads back as
 * the same paragraph instead of a heading or a list.
 */
type RenderMarkdown = (node: JSONContent, helpers: MarkdownRendererHelpers, context: RenderContext) => string

function escapeLeadingBlockMarkers(markdown: string): string {
  return markdown.replace(
    LEADING_BLOCK_MARKER,
    (marker: string, digits: string | undefined, delimiter: string | undefined) => (
      digits === undefined ? `\\${marker}` : `${digits}\\${delimiter}`
    ),
  )
}

const SuggestionStarterKit = StarterKit.extend({
  addExtensions() {
    return (this.parent?.() ?? []).map((extension) => {
      if (SUGGESTION_STARTER_BLOCK_PARENTS.has(extension.name)) {
        return extension.extend({ marks: SUGGESTION_MARKS })
      }
      if (extension.name === 'paragraph') {
        const renderParagraph = getExtensionField<RenderMarkdown>(extension, 'renderMarkdown')
        return extension.extend({
          renderMarkdown: (node: JSONContent, helpers: MarkdownRendererHelpers, context: RenderContext) => (
            escapeLeadingBlockMarkers(renderParagraph?.(node, helpers, context) ?? '')
          ),
        })
      }
      return extension
    })
  },
})

export function createEditorSchemaExtensions(
  options: EditorSchemaExtensionOptions = {},
): Extensions {
  return [
    SuggestionStarterKit.configure({
      document: false,
      link: false,
      undoRedo: options.enableUndoRedo === false ? false : {},
    }),
    Document,
    Markdown.configure({
      markedOptions: {
        gfm: true,
      },
    }),
    BlockMath.configure({
      katexOptions: {
        displayMode: true,
        throwOnError: false,
      },
    }),
    InlineMath.configure({
      katexOptions: {
        displayMode: false,
        throwOnError: false,
      },
    }),
    Link.configure(safeLinkOptions()),
    Highlight.configure({ multicolor: false }),
    TaskList,
    TaskItem.configure({ nested: true }),
    EditorTable.configure({ resizable: options.resizableTables ?? false }),
    TableRow,
    TableHeader,
    TableCell,
    OfferedTextAlign.configure({
      alignments: [...EDITOR_TEXT_ALIGNMENTS],
      types: ['heading', 'paragraph'],
    }),
    StructureSuggestionAttributes,
    SuggestionDeletion,
    SuggestionInsertion,
    SuggestionModification,
  ]
}

function safeLinkOptions() {
  return {
    HTMLAttributes: {
      rel: 'noopener noreferrer nofollow',
      target: '_blank',
    },
    autolink: true,
    defaultProtocol: 'https',
    isAllowedUri: (url: string, context: { defaultValidate: (url: string) => boolean }) => (
      isSafeEditorUrl(url) && context.defaultValidate(url)
    ),
    linkOnPaste: true,
    openOnClick: false,
    shouldAutoLink: isSafeEditorUrl,
  }
}

function isSafeEditorUrl(value: string): boolean {
  const trimmed = value.trim()
  if (!trimmed) return false
  try {
    const url = new URL(trimmed.includes(':') ? trimmed : `https://${trimmed}`)
    return url.protocol === 'http:' || url.protocol === 'https:' || url.protocol === 'mailto:'
  } catch {
    return false
  }
}
