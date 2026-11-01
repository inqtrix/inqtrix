/**
 * Client-side Markdown → Word export into a template the user chooses.
 *
 * The template keeps its cover, headers, footers, sections and styles; the
 * document's text takes the place of the `{{content}}` paragraph, and
 * `{{title}}` and `{{date}}` are replaced wherever they stand. The text is
 * mapped by the same walk as the normal export ({@link ./docxExport}), dressed
 * with the template's own styles, which are found by their name because their
 * ids differ per language (`heading 1` is `berschrift1` in a German Word).
 *
 * Everything happens in the browser: the template is neither uploaded nor kept.
 * Reached only via a dynamic `import()` from the editor toolbar, so this module
 * and its dependencies stay in their own async chunk.
 */
import {
  AlignmentType,
  convertMillimetersToTwip,
  patchDocument,
  PatchType,
  Paragraph,
  Table,
  TableProperties,
  type TableRow,
  TextRun,
  WidthType,
} from 'docx'
import JSZip from 'jszip'
import { type Element as XmlElement, js2xml, xml2js } from 'xml-js'
import {
  type DocxLook,
  DOCX_MIME,
  docxFileName,
  downloadDocxBlob,
  exportTitle,
  markdownToDocxBlocks,
  reportLook,
} from './docxExport'
import { latexReportNumbering } from './latexReportStyle'
import {
  TemplateExportError,
  type TemplateExportNotice,
} from './templateExportMessages'

export const TEMPLATE_MAX_BYTES = 32 * 1024 * 1024

const DOCUMENT_PART = 'word/document.xml'
const CONTENT_TYPES_PART = '[Content_Types].xml'
const TEMPLATE_MAIN_TYPE = 'application/vnd.openxmlformats-officedocument.wordprocessingml.template.main+xml'
const DOCUMENT_MAIN_TYPE = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml'
/** The main part of a .docm or .dotm; a macro workbook embedded in a template has another type. */
const MACRO_MAIN_TYPE = /application\/vnd\.ms-word\.[^"]*macroEnabled/i
/** What `patchDocument` adds is in the ordinary namespaces, which a strict file does not mix with. */
const STRICT_WORD_NAMESPACE = 'http://purl.oclc.org/ooxml/wordprocessingml/main'
const CONTENT_MARKER = '{{content}}'
const REPLACED_MARKERS = ['{{date}}', '{{title}}'] as const
/** Word stores a paragraph's objects here; the report must not silently take their place. */
const PARAGRAPH_OBJECTS = new Set(['w:drawing', 'w:object', 'w:pict'])
/** Heading sizes (half-points) for a template without any heading style. */
const PLAIN_HEADING_SIZES = [32, 28, 26, 24, 22, 22]

function readXml(text: string): XmlElement {
  return xml2js(text, { compact: false, captureSpacesBetweenElements: true }) as XmlElement
}

function withAmpersandsEscaped(element: XmlElement): XmlElement {
  return {
    ...element,
    ...(element.attributes ? {
      attributes: Object.fromEntries(Object.entries(element.attributes).map(
        ([name, value]) => [name, value === undefined ? value : String(value).replace(/&/g, '&amp;')],
      )),
    } : {}),
    ...(element.elements ? {
      elements: element.elements.map((child) => child.type === 'text'
        ? { ...child, text: String(child.text).replace(/&/g, '&amp;') }
        : withAmpersandsEscaped(child)),
    } : {}),
  }
}

/** Writes the part for `patchDocument` to read: xml-js would turn a literal
 * "&amp;" in a text into "&", and leave "&" in an attribute unescaped, unless
 * each "&" is escaped once more. */
function writeXml(root: XmlElement): string {
  return js2xml(withAmpersandsEscaped(root))
}

function childNamed(element: XmlElement, name: string): XmlElement | undefined {
  return element.elements?.find((child) => child.name === name)
}

function textOfElement(element: XmlElement): string {
  return (element.elements ?? []).map((child) => String(child.text ?? '')).join('')
}

/** All text of a paragraph, also below tracked changes, links and content
 * controls. A text box inside it has paragraphs of its own. */
function paragraphText(element: XmlElement): string {
  return (element.elements ?? []).map((child) => {
    if (child.name === 'w:t') return textOfElement(child)
    return child.name === 'w:p' ? '' : paragraphText(child)
  }).join('')
}

/** The text `patchDocument` can see and replace: only runs directly in the paragraph. */
function directRunText(paragraph: XmlElement): string {
  return (paragraph.elements ?? [])
    .filter((child) => child.name === 'w:r')
    .flatMap((run) => run.elements ?? [])
    .filter((child) => child.name === 'w:t')
    .map(textOfElement)
    .join('')
}

function holdsObject(element: XmlElement): boolean {
  return (element.elements ?? []).some((child) => (
    PARAGRAPH_OBJECTS.has(String(child.name)) || (child.name !== 'w:p' && holdsObject(child))
  ))
}

type LocatedParagraph = { paragraph: XmlElement; parents: XmlElement[] }

function paragraphsOf(root: XmlElement, parents: XmlElement[] = []): LocatedParagraph[] {
  return (root.elements ?? []).flatMap((child) => [
    ...(child.name === 'w:p' ? [{ paragraph: child, parents: [...parents, root] }] : []),
    ...paragraphsOf(child, [...parents, root]),
  ])
}

function occurrences(text: string, marker: string): number {
  return text.split(marker).length - 1
}

function markerParagraph(key: string): XmlElement {
  return {
    type: 'element',
    name: 'w:p',
    elements: [{
      type: 'element',
      name: 'w:r',
      elements: [{
        type: 'element',
        name: 'w:t',
        elements: [{ type: 'text', text: `{{${key}}}` }],
      }],
    }],
  }
}

/**
 * Put the report's one-time marker where the report belongs. The key cannot
 * occur in a title, so only this one paragraph takes the report, and the
 * marker is left as one simple run however Word split the user's text.
 */
function placeContentMarker(root: XmlElement, key: string): boolean {
  const located = paragraphsOf(root).filter(({ paragraph }) => (
    paragraphText(paragraph).includes(CONTENT_MARKER)
  ))
  const body = childNamed(childNamed(root, 'w:document') ?? {}, 'w:body')
  if (!body) throw new TemplateExportError('template_unreadable')
  if (located.length === 0) {
    const elements = body.elements ?? []
    const sectionIndex = elements.map((child) => child.name).lastIndexOf('w:sectPr')
    elements.splice(sectionIndex === -1 ? elements.length : sectionIndex, 0, markerParagraph(key))
    body.elements = elements
    return false
  }
  if (located.length > 1 || occurrences(paragraphText(located[0].paragraph), CONTENT_MARKER) > 1) {
    throw new TemplateExportError('content_marker_repeated')
  }
  const { paragraph, parents } = located[0]
  if (paragraphText(paragraph).trim() !== CONTENT_MARKER || holdsObject(paragraph)) {
    throw new TemplateExportError('content_marker_not_alone')
  }
  const parent = parents[parents.length - 1]
  const siblings = parent.elements ?? []
  const properties = childNamed(paragraph, 'w:pPr')
  if (properties && childNamed(properties, 'w:sectPr')) {
    // Word keeps a section's break in its last paragraph: it has to stay.
    paragraph.elements = [properties]
    siblings.splice(siblings.indexOf(paragraph), 0, markerParagraph(key))
    return true
  }
  const control = parents[parents.length - 2]
  const holder = parents[parents.length - 3]
  if (
    parent.name === 'w:sdtContent'
    && control?.name === 'w:sdt'
    && holder?.elements
    && siblings.filter((child) => child.type === 'element').length === 1
  ) {
    // A control holding only the marker goes: a plain text control takes one paragraph.
    holder.elements.splice(holder.elements.indexOf(control), 1, markerParagraph(key))
    return true
  }
  siblings.splice(siblings.indexOf(paragraph), 1, markerParagraph(key))
  return true
}

function namedStyles(stylesRoot: XmlElement | undefined): Map<string, string> {
  const styles = new Map<string, string>()
  for (const style of (stylesRoot && childNamed(stylesRoot, 'w:styles'))?.elements ?? []) {
    if (style.name !== 'w:style') continue
    const name = childNamed(style, 'w:name')?.attributes?.['w:val']
    const id = style.attributes?.['w:styleId']
    const type = style.attributes?.['w:type']
    if (name !== undefined && id !== undefined) {
      styles.set(`${String(type)}:${String(name).toLowerCase()}`, String(id))
    }
  }
  return styles
}

/** A table in a style of the template. `Table` always writes direct borders,
 * which win over a table style, so this one writes its properties without. */
class StyledTable extends Table {
  constructor(style: string, rows: TableRow[]) {
    const width = { size: 100, type: WidthType.PERCENTAGE }
    super({ rows, width })
    this.root[0] = new TableProperties({ style, width, tableLook: { firstRow: true, noVBand: true } })
  }
}

function templateLook(styles: Map<string, string>) {
  const paragraphStyle = (name: string) => styles.get(`paragraph:${name}`)
  const headingIds = [1, 2, 3, 4, 5, 6].map((level) => paragraphStyle(`heading ${level}`))
  const listParagraph = paragraphStyle('list paragraph')
  const quote = paragraphStyle('quote')
  const code = paragraphStyle('inqtrix code')
  const hyperlink = styles.get('character:hyperlink')
  const tableStyle = styles.get('table:inqtrix table') ?? styles.get('table:table grid')
  const borrowedLevels = new Set<number>()
  let headingsWithoutStyle = false

  const look: DocxLook = {
    heading: (depth) => {
      const own = headingIds[depth - 1]
      if (own) return { paragraph: { style: own } }
      const nearest = [...headingIds.slice(0, depth - 1)].reverse().find(Boolean)
        ?? headingIds.slice(depth).find(Boolean)
      if (nearest) {
        borrowedLevels.add(depth)
        return { paragraph: { style: nearest, outlineLevel: depth - 1 } }
      }
      headingsWithoutStyle = true
      return {
        paragraph: { keepNext: true, outlineLevel: depth - 1 },
        marks: { bold: true, size: PLAIN_HEADING_SIZES[depth - 1] },
      }
    },
    listItem: listParagraph ? { style: listParagraph } : {},
    listContinuation: (level) => ({
      ...(listParagraph ? { style: listParagraph } : {}),
      indent: { left: convertMillimetersToTwip(8 * (level + 1)) },
    }),
    codeLine: code
      ? { paragraph: { style: code }, run: {} }
      : {
          paragraph: { alignment: AlignmentType.LEFT, spacing: { before: 0, after: 0 } },
          run: reportLook.inlineCode,
        },
    quote: quote ? { style: quote } : { indent: { left: convertMillimetersToTwip(8) } },
    inlineCode: reportLook.inlineCode,
    link: hyperlink ? { style: hyperlink } : reportLook.link,
    thematicBreak: { spacing: { before: 120, after: 120 } },
    tableCell: tableStyle ? () => ({}) : reportLook.tableCell,
    tableCellParagraph: tableStyle ? {} : reportLook.tableCellParagraph,
    table: (rows) => (tableStyle ? new StyledTable(tableStyle, rows) : reportLook.table(rows)),
  }

  return {
    look,
    notices: (): TemplateExportNotice[] => [
      ...(headingsWithoutStyle ? [{ code: 'heading_styles_missing' as const }] : []),
      ...(borrowedLevels.size > 0
        ? [{ code: 'heading_levels_borrowed' as const, levels: [...borrowedLevels].sort() }]
        : []),
    ],
  }
}

/** The zip of a Word file, or the refusal the user can act on. */
async function openTemplate(template: Uint8Array): Promise<JSZip> {
  let zip: JSZip
  try {
    zip = await JSZip.loadAsync(template)
  } catch {
    throw new TemplateExportError('template_unreadable')
  }
  // `patchDocument` writes a folder entry back as an empty file with that name.
  for (const [name, entry] of Object.entries(zip.files)) {
    if (entry.dir) delete zip.files[name]
  }
  const contentTypes = await zip.file(CONTENT_TYPES_PART)?.async('string')
  if (!contentTypes || !zip.file(DOCUMENT_PART)) throw new TemplateExportError('template_unreadable')
  if (zip.file('word/vbaProject.bin') || MACRO_MAIN_TYPE.test(contentTypes)) {
    throw new TemplateExportError('template_has_macros')
  }
  if (contentTypes.includes(TEMPLATE_MAIN_TYPE)) {
    // A .dotx differs from a .docx only in this line, and Word refuses a mix.
    zip.file(
      CONTENT_TYPES_PART,
      contentTypes.replace(TEMPLATE_MAIN_TYPE, () => DOCUMENT_MAIN_TYPE),
      { createFolders: false },
    )
  }
  return zip
}

/** Names the markers that would stay visible, because `patchDocument` only
 * sees runs directly in a paragraph, not in a tracked change, a link or an
 * inline content control. */
function markersOutOfReach(roots: XmlElement[]): string[] {
  const paragraphs = roots.flatMap((root) => paragraphsOf(root))
  return REPLACED_MARKERS.filter((marker) => paragraphs.some(({ paragraph }) => (
    occurrences(paragraphText(paragraph), marker) > occurrences(directRunText(paragraph), marker)
  )))
}

/**
 * Build the finished Word file. Pure and DOM-free so the unit tests can read the
 * XML it writes.
 *
 * Args:
 *   template: the bytes of the `.docx` or `.dotx` the user chose.
 *   markdown: the document's canonical markdown source.
 *   title: the document title, for `{{title}}`.
 *   options: the UI's locale and the moment of the export, for `{{date}}`.
 *
 * Returns:
 *   The file's bytes, and what the user should know about how it came out.
 *
 * Raises:
 *   TemplateExportError: the template cannot take the report; its code names why.
 */
export async function buildDocxFromTemplate(
  template: Uint8Array,
  markdown: string,
  title: string,
  options: { locale: 'de' | 'en'; now: Date },
): Promise<{ bytes: Uint8Array; notices: TemplateExportNotice[] }> {
  const zip = await openTemplate(template)
  const otherParts = await Promise.all(Object.keys(zip.files)
    .filter((name) => name.startsWith('word/') && name.endsWith('.xml') && name !== DOCUMENT_PART)
    .map(async (name) => ({ name, root: readXml(await zip.file(name)!.async('string')) })))
  const otherRoots = otherParts.map(({ root }) => root)
  if (otherRoots.some((root) => (
    paragraphsOf(root).some(({ paragraph }) => paragraphText(paragraph).includes(CONTENT_MARKER))
  ))) {
    throw new TemplateExportError('content_marker_outside_body')
  }

  const documentXml = await zip.file(DOCUMENT_PART)!.async('string')
  if (documentXml.includes(STRICT_WORD_NAMESPACE)) throw new TemplateExportError('template_is_strict')
  const documentRoot = readXml(documentXml)
  const contentKey = `inqtrix-content-${Math.random().toString(36).slice(2)}`
  const markerFound = placeContentMarker(documentRoot, contentKey)
  const unreachable = markersOutOfReach([documentRoot, ...otherRoots])
  zip.file(DOCUMENT_PART, writeXml(documentRoot), { createFolders: false })

  const { look, notices } = templateLook(namedStyles(
    otherParts.find(({ name }) => name === 'word/styles.xml')?.root,
  ))
  const blocks = markdownToDocxBlocks(markdown, look)
  // Later patches also search what an earlier one inserted: the date cannot hold a
  // marker, a title can, and the report, which may quote any of them, comes last.
  const bytes = await patchDocument({
    outputType: 'uint8array',
    data: await zip.generateAsync({ type: 'uint8array' }),
    keepOriginalStyles: true,
    numbering: latexReportNumbering,
    patches: {
      date: {
        type: PatchType.PARAGRAPH,
        children: [new TextRun(options.now.toLocaleDateString(options.locale))],
      },
      title: { type: PatchType.PARAGRAPH, children: [new TextRun(exportTitle(title))] },
      [contentKey]: {
        type: PatchType.DOCUMENT,
        children: blocks.length > 0 ? blocks : [new Paragraph({})],
      },
    },
  })
  return {
    bytes,
    notices: [
      ...(markerFound ? [] : [{ code: 'content_marker_missing' as const }]),
      ...notices(),
      ...(unreachable.length > 0 ? [{ code: 'markers_not_replaced' as const, markers: unreachable }] : []),
    ],
  }
}

/**
 * Export the markdown into the chosen template and trigger a browser download.
 * Rejects (never swallows) on failure so the caller can surface it.
 *
 * Returns:
 *   What the user should know about how the file came out.
 */
export async function exportMarkdownToDocxTemplate(
  file: File,
  markdown: string,
  title: string,
  locale: 'de' | 'en',
): Promise<TemplateExportNotice[]> {
  if (file.size > TEMPLATE_MAX_BYTES) throw new TemplateExportError('template_too_large')
  const now = new Date()
  const { bytes, notices } = await buildDocxFromTemplate(
    new Uint8Array(await file.arrayBuffer()),
    markdown,
    title,
    { locale, now },
  )
  downloadDocxBlob(new Blob([bytes as BlobPart], { type: DOCX_MIME }), docxFileName(title, now))
  return notices
}
