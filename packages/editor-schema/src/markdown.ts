import type { JSONContent } from '@tiptap/core'
import { getSchema } from '@tiptap/core'
import { MarkdownManager } from '@tiptap/markdown'
import type { Schema } from '@tiptap/pm/model'
import { createEditorSchemaExtensions } from './extensions.js'
import { projectFinalDocument, projectOriginalDocument } from './suggestions.js'

const TIPTAP_TABLE_CELL_ARTIFACT = String.fromCharCode(0x1f)

let markdownManager: MarkdownManager | null = null

export type EditorProjectionMode = 'final' | 'original'

function getMarkdownManager(): MarkdownManager {
  markdownManager ??= new MarkdownManager({
    extensions: createEditorSchemaExtensions({ enableUndoRedo: false }),
    markedOptions: { gfm: true },
  })
  return markdownManager
}

/** The import rule for FOREIGN Markdown: model output, clipboard, import.
 *
 * Rewrites the LaTeX delimiters `\[…\]` and `\(…\)` into the `$$`/`$` form
 * the editor understands. It belongs EXCLUSIVELY on incoming foreign text.
 *
 * Applied to the editor's own serialization it destroys content: the
 * serializer escapes every literal square bracket as `\[` / `\]` (citation
 * labels are the exception, written raw by
 * :func:`serializeWithRawCitationLabels` wherever a raw bracket keeps the
 * meaning), and this rule reads that sequence as formula delimiters.
 * `Ein Platzhalter [Marke] im Satz.` would become a formula block plus two
 * paragraphs, and the next save would lose the original text. The canonical
 * reading path (:func:`parseEditorMarkdown`) therefore never calls it. */
export function normalizeEditorMarkdown(markdown: string): string {
  return sanitizeSerializedEditorMarkdown(markdown)
    .replace(/\\\[([\s\S]*?)\\\]/g, (_match, expression: string) => (
      `\n\n$$\n${expression.trim()}\n$$\n\n`
    ))
    .replace(/\\\(([\s\S]*?)\\\)/g, (_match, expression: string) => (
      `$${expression.trim()}$`
    ))
}

/** Markdown as stored, without the separator (U+001F) that older versions of
 * the Markdown serializer left inside populated table cells. The serializer
 * no longer writes it, but documents stored with it still carry it, and it
 * breaks the table when read; every reader of stored Markdown removes it. */
export function sanitizeSerializedEditorMarkdown(markdown: string): string {
  return markdown.split(TIPTAP_TABLE_CELL_ARTIFACT).join('')
}

/** Kanonisches Lesen EIGENER Markdown — die Umkehrung von
 * :func:`serializeEditorJson`.
 *
 * Bewusst OHNE die Einfuhr-Regel: was hier hereinkommt, hat dieses Paket
 * selbst geschrieben, die Bedeutung war beim Schreiben bereits entschieden.
 * Fremder Text laeuft vorher durch :func:`normalizeEditorMarkdown`. */
export function parseEditorMarkdown(markdown: string): JSONContent {
  const parsed = getMarkdownManager().parse(sanitizeSerializedEditorMarkdown(markdown))
  // MarkdownManager currently returns an empty doc for an empty string, but
  // the editor schema requires at least one block. Empty editor documents are
  // valid product state and must be convertible to collaboration without a
  // misleading `invalid_schema` conflict.
  if (parsed.type === 'doc' && (!parsed.content || parsed.content.length === 0)) {
    return canonicalizeEditorJson({
      type: 'doc',
      content: [{ type: 'paragraph' }],
    })
  }
  return canonicalizeEditorJson(parsed)
}

export function serializeEditorJson(
  content: JSONContent,
  projection: EditorProjectionMode = 'final',
): string {
  const schema = getSchema(createEditorSchemaExtensions({ enableUndoRedo: false }))
  const document = schema.nodeFromJSON(content)
  const projected = projection === 'final'
    ? projectFinalDocument(document)
    : projectOriginalDocument(document)
  const manager = getMarkdownManager()
  return serializeWithRawCitationLabels(
    projected.toJSON(),
    schema,
    (json) => manager.serialize(json),
  )
}

// Internal placeholders for the brackets of a citation label while the
// Markdown serializer runs. Unicode noncharacters: reserved for internal use,
// never produced by the serializer, and left untouched by its escaping and
// entity encoding.
const CITATION_OPEN = '\uFDD0'
const CITATION_CLOSE = '\uFDD1'
const CITATION_PLACEHOLDER = /[\uFDD0\uFDD1]/
// One bracket of citation labels, single or grouped (`[K1]`, `[K2, W7]`):
// the label grammar of the citation readers without a family restriction.
// A bracket whose `[` follows a backslash in the text is not one.
const CITATION_LABEL = '[A-Z][0-9]+'
const CITATION_BRACKET = new RegExp(
  `(?<!\\\\)\\[([ \\t]*${CITATION_LABEL}(?:[ \\t]*[,;][ \\t]*${CITATION_LABEL})*[ \\t]*)\\]`,
  'g',
)
const PROTECTED_CITATION = /\uFDD0([^\uFDD0\uFDD1]*)\uFDD1/g
// What may stand before a link reference definition on its line: block
// quote and list markers (a task checkbox included) and indentation.
const CONTAINER_MARKERS = /^(?:[ \t]*(?:>|[-*+]|[0-9]{1,9}[.)]|\[[ xX]\]))*[ \t]*$/
const LINK_MARK = 'link'

function warnLabelsLeftEscapedOnConsole(): void {
  console.warn(
    'Citation labels stay escaped in this Markdown: the document already contains '
    + 'an internal placeholder character (U+FDD0 or U+FDD1).',
  )
}

let reportLabelsLeftEscaped: () => void = warnLabelsLeftEscapedOnConsole

/**
 * Set who is told when :func:`serializeWithRawCitationLabels` leaves the
 * citation labels of a document escaped; `null` restores the default, a
 * console warning. A service that writes structured logs installs its own
 * reporter once at startup.
 */
export function setCitationLabelsLeftEscapedReporter(report: (() => void) | null): void {
  reportLabelsLeftEscaped = report ?? warnLabelsLeftEscapedOnConsole
}

/**
 * Serialize *content* with its citation labels written raw.
 *
 * `@tiptap/markdown` backslash-escapes every `[` and `]` of non-code text,
 * so a stored label would read `\[K1\]`, which every citation reader takes
 * for literal text. The brackets of each label in plain text therefore pass
 * the serializer as placeholders and are written back afterwards: raw, except
 * where a raw bracket would change what the Markdown means. Before `(` it
 * would open an inline link, and followed by `:` with only container markers
 * before it on its line it would open a link reference definition; there the
 * escaped form stays. Code (a node or mark whose schema spec says `code`, the
 * criterion the serializer uses for its own escaping) and link text are
 * never touched. Parsing is unchanged: the document read back is the one the
 * escaping serializer would have produced. An exception is a label glued to
 * a bare URL or `www.` address (`https://x.org/[K1]`): a GFM autolink takes
 * it into the link in either spelling, and the raw one keeps the text free of
 * backslashes.
 *
 * A document that already holds a placeholder character is serialized
 * unprotected, and the reporter set by
 * :func:`setCitationLabelsLeftEscapedReporter` is told: its labels stay
 * escaped.
 */
export function serializeWithRawCitationLabels(
  content: JSONContent,
  schema: Schema,
  serialize: (content: JSONContent) => string,
): string {
  if (CITATION_PLACEHOLDER.test(JSON.stringify(content))) {
    reportLabelsLeftEscaped()
    return serialize(content)
  }
  return restoreCitationLabels(
    serialize(protectCitationLabels(content, codeTypeNames(schema), false)),
  )
}

function codeTypeNames(schema: Schema): ReadonlySet<string> {
  return new Set([
    ...Object.values(schema.nodes).filter((type) => type.spec.code).map((type) => type.name),
    ...Object.values(schema.marks).filter((type) => type.spec.code).map((type) => type.name),
  ])
}

function protectCitationLabels(
  node: JSONContent,
  codeTypes: ReadonlySet<string>,
  insideCode: boolean,
): JSONContent {
  if (node.type === 'text') {
    if (
      insideCode
      || !node.text
      || node.marks?.some((mark) => mark.type === LINK_MARK || codeTypes.has(mark.type))
    ) return node
    return {
      ...node,
      text: node.text.replace(
        CITATION_BRACKET,
        (_bracket, inner: string) => `${CITATION_OPEN}${inner}${CITATION_CLOSE}`,
      ),
    }
  }
  if (!node.content) return node
  const code = insideCode || codeTypes.has(node.type ?? '')
  return {
    ...node,
    content: node.content.map((child) => protectCitationLabels(child, codeTypes, code)),
  }
}

function restoreCitationLabels(markdown: string): string {
  return markdown.replace(
    PROTECTED_CITATION,
    (placeholder: string, inner: string, offset: number) => {
      const next = markdown[offset + placeholder.length]
      const opensDefinition = next === ':' && onlyContainerMarkersBefore(markdown, offset)
      return next === '(' || opensDefinition ? `\\[${inner}\\]` : `[${inner}]`
    },
  )
}

/** Whether only container markers stand before *offset* on its line. Looks
 * back no further than that line, so restoring stays linear in the text. */
function onlyContainerMarkersBefore(markdown: string, offset: number): boolean {
  const line = markdown.slice(markdown.lastIndexOf('\n', offset - 1) + 1, offset)
  return CONTAINER_MARKERS.test(line.slice(line.lastIndexOf('\r') + 1))
}

export function canonicalizeEditorJson(content: JSONContent): JSONContent {
  const schema = getSchema(createEditorSchemaExtensions({ enableUndoRedo: false }))
  return schema.nodeFromJSON(content).toJSON()
}
