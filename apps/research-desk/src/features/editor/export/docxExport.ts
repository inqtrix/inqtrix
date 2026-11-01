/**
 * Client-side Markdown → Word (.docx) export in a professional LaTeX-report
 * style.
 *
 * The document markdown is parsed to an mdast tree with the unified/remark
 * stack (the same ecosystem react-markdown already uses) and walked into
 * `docx` primitives by an in-house mapper — there is no Markdown→HTML step, so
 * raw HTML never reaches a DOM and offers no injection surface (raw `html`
 * nodes are skipped outright). Math is degraded to its LaTeX source text
 * (KaTeX is not reproduced in Word). The visual style (serif body justified,
 * numbered headings, title block, page numbers) lives in {@link ./latexReportStyle}.
 *
 * Both libraries (`docx`, unified/remark) are loaded lazily: this module is
 * only ever reached via a dynamic `import()` from the editor toolbar, so it and
 * its dependencies are code-split into their own async chunk.
 */
import {
  AlignmentType,
  BorderStyle,
  convertMillimetersToTwip,
  Document,
  ExternalHyperlink,
  HeadingLevel,
  Packer,
  Paragraph,
  ShadingType,
  Table,
  TableCell,
  TableRow,
  TextRun,
  UnderlineType,
  WidthType,
} from 'docx'
import type { IParagraphOptions, IPropertiesOptions, IRunOptions, ITableCellOptions } from 'docx'
import type * as md from 'mdast'
import remarkGfm from 'remark-gfm'
import remarkParse from 'remark-parse'
import { unified } from 'unified'
import {
  BULLET_LIST_REF,
  CODE_FILL,
  HEADING_NUMBERING_REF,
  latexReportNumbering,
  latexReportSection,
  latexReportStyles,
  LINE_SPACING,
  MONO_FONT,
  ORDERED_LIST_REF,
} from './latexReportStyle'
import { fileNameTimestamp } from './fileNameTimestamp'
import { aiDisclosureDocumentProperties } from '@/lib/aiDisclosure'

export const DOCX_MIME = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'

/** AI-generation disclosure for an export of model-generated content. The
 * sentence is passed in already localized: this module never imports the
 * i18n dictionary, so it stays in its own lazily loaded chunk. */
export type AiExportDisclosure = {
  notice: string
  model?: string
}

/**
 * Strip paired math delimiters so formulas survive as readable LaTeX source
 * text. `$$…$$`, `\[…\]` and `\(…\)` are always unwrapped; inline `$…$` is only
 * unwrapped when the content looks like math (contains `\ ^ _ { } =`) so that
 * currency like "$5" is left untouched.
 */
export function degradeMathDelimiters(text: string): string {
  return text
    .replace(/\$\$([\s\S]+?)\$\$/g, (_match, expression: string) => expression.trim())
    .replace(/\\\[([\s\S]+?)\\\]/g, (_match, expression: string) => expression.trim())
    .replace(/\\\(([\s\S]+?)\\\)/g, (_match, expression: string) => expression.trim())
    .replace(/\$([^$\n]+?)\$/g, (match: string, expression: string) =>
      /[\\^_{}=]/.test(expression) ? expression.trim() : match)
}

/** Lower-case, ASCII-folded, hyphenated filename segment. */
function sanitizeFileNameSegment(value: string): string {
  return value
    .normalize('NFKD')
    .replace(/[̀-ͯ]/g, '')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
}

/**
 * Derive a short, collision-resistant filename: a title slug capped at 40
 * characters plus a date-time stamp, e.g. `gemini-als-ki-20260601-090705.docx`.
 */
export function docxFileName(title: string, date: Date): string {
  const slug = sanitizeFileNameSegment(title).slice(0, 40).replace(/-+$/, '')
  return `${slug || 'dokument'}-${fileNameTimestamp(date)}.docx`
}

export type Marks = {
  bold?: boolean
  italics?: boolean
  strike?: boolean
  code?: boolean
  link?: boolean
  size?: number
}

export type ParagraphDress = Omit<IParagraphOptions, 'children'>
export type RunDress = Pick<IRunOptions, 'color' | 'font' | 'shading' | 'style' | 'underline'>

/**
 * What a block or a span looks like in the file. The walk over the Markdown is
 * one, so a report and an export into a Word template keep the same content;
 * only the dress differs.
 */
export type DocxLook = {
  heading: (depth: number) => { paragraph: ParagraphDress; marks?: Marks }
  listItem: ParagraphDress
  listContinuation: (level: number) => ParagraphDress
  codeLine: { paragraph: ParagraphDress; run: RunDress }
  quote: ParagraphDress
  inlineCode: RunDress
  link: RunDress
  thematicBreak: ParagraphDress
  tableCell: (isHeader: boolean) => Pick<ITableCellOptions, 'borders' | 'margins'>
  tableCellParagraph: ParagraphDress
  table: (rows: TableRow[]) => Table
}

const HEADING_LEVELS = [
  HeadingLevel.HEADING_1,
  HeadingLevel.HEADING_2,
  HeadingLevel.HEADING_3,
  HeadingLevel.HEADING_4,
  HeadingLevel.HEADING_5,
  HeadingLevel.HEADING_6,
] as const

/** The professional LaTeX-report look of {@link ./latexReportStyle}. */
export const reportLook: DocxLook = {
  heading: (depth) => ({
    paragraph: depth <= 4
      ? {
          heading: HEADING_LEVELS[depth - 1],
          numbering: { reference: HEADING_NUMBERING_REF, level: depth - 1 },
        }
      : { heading: HEADING_LEVELS[depth - 1] },
  }),
  listItem: { alignment: AlignmentType.LEFT, spacing: { after: 0, line: LINE_SPACING } },
  listContinuation: (level) => ({
    alignment: AlignmentType.LEFT,
    spacing: { after: 0, line: LINE_SPACING },
    indent: { firstLine: 0, left: convertMillimetersToTwip(8 * (level + 1)) },
  }),
  codeLine: { paragraph: { style: 'InqtrixCode' }, run: {} },
  quote: { style: 'InqtrixQuote' },
  inlineCode: {
    font: MONO_FONT,
    shading: { type: ShadingType.CLEAR, color: 'auto', fill: CODE_FILL },
  },
  link: { color: '0563C1', underline: { type: UnderlineType.SINGLE } },
  // A markdown `---` is a section separator, not a rule: render it as a clean
  // vertical gap with no border line (the numbered headings already structure
  // the report). Avoids stray separator lines in the exported document.
  thematicBreak: { indent: { firstLine: 0 }, spacing: { before: 120, after: 120 } },
  tableCell: (isHeader) => ({
    margins: { top: 40, bottom: 40, left: 80, right: 80 },
    borders: isHeader
      ? { bottom: { style: BorderStyle.SINGLE, size: 6, color: '000000' } }
      : undefined,
  }),
  tableCellParagraph: {
    alignment: AlignmentType.LEFT,
    indent: { firstLine: 0 },
    spacing: { after: 0, line: LINE_SPACING },
  },
  table: (rows) => new Table({
    width: { size: 100, type: WidthType.PERCENTAGE },
    borders: {
      top: { style: BorderStyle.SINGLE, size: 8, color: '000000' },
      bottom: { style: BorderStyle.SINGLE, size: 8, color: '000000' },
      left: { style: BorderStyle.NONE, size: 0, color: 'auto' },
      right: { style: BorderStyle.NONE, size: 0, color: 'auto' },
      insideHorizontal: { style: BorderStyle.NONE, size: 0, color: 'auto' },
      insideVertical: { style: BorderStyle.NONE, size: 0, color: 'auto' },
    },
    rows,
  }),
}

function makeRun(text: string, marks: Marks, look: DocxLook): TextRun {
  return new TextRun({
    text,
    bold: marks.bold,
    italics: marks.italics,
    strike: marks.strike,
    size: marks.size,
    ...(marks.code ? look.inlineCode : undefined),
    ...(marks.link ? look.link : undefined),
  })
}

function inlineToRuns(
  nodes: md.PhrasingContent[],
  look: DocxLook,
  marks: Marks = {},
): (TextRun | ExternalHyperlink)[] {
  const runs: (TextRun | ExternalHyperlink)[] = []
  for (const node of nodes) {
    switch (node.type) {
      case 'text':
        runs.push(makeRun(degradeMathDelimiters(node.value), marks, look))
        break
      case 'strong':
        runs.push(...inlineToRuns(node.children, look, { ...marks, bold: true }))
        break
      case 'emphasis':
        runs.push(...inlineToRuns(node.children, look, { ...marks, italics: true }))
        break
      case 'delete':
        runs.push(...inlineToRuns(node.children, look, { ...marks, strike: true }))
        break
      case 'inlineCode':
        runs.push(makeRun(node.value, { ...marks, code: true }, look))
        break
      case 'break':
        runs.push(new TextRun({ break: 1 }))
        break
      case 'link': {
        const childRuns = inlineToRuns(node.children, look, { ...marks, link: true }).filter(
          (run): run is TextRun => run instanceof TextRun,
        )
        runs.push(new ExternalHyperlink({ link: node.url, children: childRuns }))
        break
      }
      case 'image':
        runs.push(makeRun(`[${node.alt?.trim() || 'Bild'}]`, { ...marks, italics: true }, look))
        break
      case 'html':
        break
      default:
        if ('children' in node && Array.isArray((node as { children?: unknown }).children)) {
          runs.push(...inlineToRuns((node as unknown as { children: md.PhrasingContent[] }).children, look, marks))
        } else if ('value' in node && typeof (node as { value?: unknown }).value === 'string') {
          runs.push(makeRun((node as unknown as { value: string }).value, marks, look))
        }
        break
    }
  }
  return runs
}

type WalkContext = { orderedInstance: number; look: DocxLook }

function headingToDocx(node: md.Heading, look: DocxLook): Paragraph {
  const depth = Math.min(Math.max(node.depth, 1), 6)
  const dress = look.heading(depth)
  return new Paragraph({ ...dress.paragraph, children: inlineToRuns(node.children, look, dress.marks) })
}

function appendListItem(
  item: md.ListItem,
  ctx: WalkContext,
  depth: number,
  ordered: boolean,
  instance: number,
  out: Paragraph[],
): void {
  const level = Math.min(depth, 3)
  let markerUsed = false
  for (const child of item.children) {
    if (child.type === 'list') {
      out.push(...listToParagraphs(child, ctx, depth + 1))
      continue
    }
    if (child.type === 'paragraph') {
      const children = inlineToRuns(child.children, ctx.look)
      if (markerUsed) {
        out.push(new Paragraph({ ...ctx.look.listContinuation(level), children }))
      } else {
        out.push(new Paragraph({
          ...ctx.look.listItem,
          numbering: ordered
            ? { reference: ORDERED_LIST_REF, level, instance }
            : { reference: BULLET_LIST_REF, level },
          children,
        }))
        markerUsed = true
      }
      continue
    }
    if (child.type === 'code') {
      out.push(...codeToParagraphs(child, ctx.look))
      continue
    }
    if (child.type === 'blockquote') {
      out.push(...blockquoteToParagraphs(child, ctx))
    }
  }
}

function listToParagraphs(node: md.List, ctx: WalkContext, depth: number): Paragraph[] {
  const ordered = Boolean(node.ordered)
  const instance = ordered ? ctx.orderedInstance++ : 0
  const out: Paragraph[] = []
  for (const item of node.children) {
    appendListItem(item, ctx, depth, ordered, instance, out)
  }
  return out
}

function codeToParagraphs(node: md.Code, look: DocxLook): Paragraph[] {
  return node.value.split('\n').map((line) => new Paragraph({
    ...look.codeLine.paragraph,
    children: [new TextRun({ ...look.codeLine.run, text: line.length > 0 ? line : ' ' })],
  }))
}

function blockquoteToParagraphs(node: md.Blockquote, ctx: WalkContext): Paragraph[] {
  const out: Paragraph[] = []
  for (const child of node.children) {
    if (child.type === 'paragraph') {
      out.push(new Paragraph({ ...ctx.look.quote, children: inlineToRuns(child.children, ctx.look) }))
    } else if (child.type === 'blockquote') {
      out.push(...blockquoteToParagraphs(child, ctx))
    } else if (child.type === 'list') {
      out.push(...listToParagraphs(child, ctx, 0))
    } else if (child.type === 'code') {
      out.push(...codeToParagraphs(child, ctx.look))
    }
  }
  return out
}

function tableToDocx(node: md.Table, look: DocxLook): Table {
  return look.table(node.children.map((row, rowIndex) => new TableRow({
    tableHeader: rowIndex === 0,
    children: row.children.map((cell) => new TableCell({
      ...look.tableCell(rowIndex === 0),
      children: [new Paragraph({
        ...look.tableCellParagraph,
        children: inlineToRuns(cell.children, look, rowIndex === 0 ? { bold: true } : {}),
      })],
    })),
  })))
}

function blocksToDocx(nodes: md.RootContent[], ctx: WalkContext): (Paragraph | Table)[] {
  const out: (Paragraph | Table)[] = []
  for (const node of nodes) {
    switch (node.type) {
      case 'heading':
        out.push(headingToDocx(node, ctx.look))
        break
      case 'paragraph':
        out.push(new Paragraph({ children: inlineToRuns(node.children, ctx.look) }))
        break
      case 'list':
        out.push(...listToParagraphs(node, ctx, 0))
        break
      case 'code':
        out.push(...codeToParagraphs(node, ctx.look))
        break
      case 'blockquote':
        out.push(...blockquoteToParagraphs(node, ctx))
        break
      case 'table':
        out.push(tableToDocx(node, ctx.look))
        break
      case 'thematicBreak':
        out.push(new Paragraph({ ...ctx.look.thematicBreak, children: [] }))
        break
      default:
        // html / definition / yaml / footnoteDefinition → intentionally skipped.
        break
    }
  }
  return out
}

/**
 * Parse markdown and map it to an array of top-level docx blocks
 * (`Paragraph`/`Table`). Pure and DOM-free — the unit-tested entry point.
 */
export function markdownToDocxBlocks(markdown: string, look: DocxLook = reportLook): (Paragraph | Table)[] {
  const tree = unified().use(remarkParse).use(remarkGfm).parse(markdown) as md.Root
  return blocksToDocx(tree.children, { orderedInstance: 0, look })
}

/** A document without a title still names itself in the file. */
export function exportTitle(title: string): string {
  return title.trim() || 'Dokument'
}

function buildTitleBlock(title: string): Paragraph[] {
  return [
    new Paragraph({ heading: HeadingLevel.TITLE, children: [new TextRun(exportTitle(title))] }),
    new Paragraph({ style: 'InqtrixSubtitle', children: [new TextRun(new Date().toLocaleDateString())] }),
  ]
}

export function downloadDocxBlob(blob: Blob, fileName: string): void {
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = fileName
  link.style.display = 'none'
  document.body.appendChild(link)
  link.click()
  document.body.removeChild(link)
  window.setTimeout(() => URL.revokeObjectURL(url), 0)
}

/**
 * Build the full `Document` options for an export. Pure and DOM-free so the
 * metadata can be asserted in a unit test without running `Packer`.
 *
 * Args:
 *   markdown: the document's canonical markdown source.
 *   title: the document title, used for the title block.
 *   disclosure: AI-generation disclosure for model-generated content. When
 *     given, the notice goes into the page footer, into the core properties
 *     a reader sees in Word and Explorer, and into `docProps/custom.xml` as
 *     machine-readable custom properties. Omitted for documents the user
 *     wrote themselves — the editor performs assistive editing on the
 *     user's own text, so stamping their document as machine-generated
 *     would be inaccurate.
 */
export function docxDocumentOptions(
  markdown: string,
  title: string,
  disclosure?: AiExportDisclosure,
): IPropertiesOptions {
  const properties = disclosure
    ? aiDisclosureDocumentProperties(disclosure.notice, disclosure.model)
    : undefined
  return {
    title: title.trim() || undefined,
    ...properties?.core,
    ...(properties ? { customProperties: properties.custom } : {}),
    styles: latexReportStyles,
    numbering: latexReportNumbering,
    sections: [latexReportSection(
      [...buildTitleBlock(title), ...markdownToDocxBlocks(markdown)],
      disclosure?.notice,
    )],
  }
}

/**
 * Convert the document markdown to a styled .docx and trigger a browser
 * download. Rejects (never swallows) on failure so the caller can surface it.
 *
 * Args:
 *   markdown: the document's canonical markdown source.
 *   title: the document title, used for the title block and the filename.
 *   disclosure: see {@link docxDocumentOptions}. The caller supplies the
 *     already-localized sentence.
 */
export async function exportMarkdownToDocx(
  markdown: string,
  title: string,
  disclosure?: AiExportDisclosure,
): Promise<void> {
  const document_ = new Document(docxDocumentOptions(markdown, title, disclosure))
  const generated = await Packer.toBlob(document_)
  downloadDocxBlob(new Blob([generated], { type: DOCX_MIME }), docxFileName(title, new Date()))
}
