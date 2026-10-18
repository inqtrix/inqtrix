/**
 * docx styling for the "professional LaTeX report" Word export.
 *
 * Supplies the document-level `styles` and `numbering` plus a section builder
 * consumed by {@link ./docxExport}. The result mirrors a LaTeX `article`/
 * `report`: a serif body set justified, multi-level numbered headings
 * (1, 1.1, 1.1.1), a centered title block, A4 page with generous margins, and
 * a centered page number in the footer. Fonts are referenced by name only —
 * docx font embedding is unreliable (names with spaces corrupt the file) and
 * bloats the output, so we rely on the viewer's font substitution instead.
 */
import {
  AlignmentType,
  BorderStyle,
  convertMillimetersToTwip,
  Footer,
  type INumberingOptions,
  type ISectionOptions,
  type IStylesOptions,
  LevelFormat,
  PageNumber,
  Paragraph,
  ShadingType,
  TextRun,
} from 'docx'

/**
 * Body and heading typeface. "Times New Roman" is the default because it is a
 * serif available on effectively every platform (and metric-compatible with
 * Liberation Serif in LibreOffice), so the export reliably reads as a serif
 * academic report everywhere. Change this single constant to "Latin Modern
 * Roman" for an authentic LaTeX look where that font is installed.
 */
export const SERIF_FONT = 'Times New Roman'
/** Monospace face for code spans/blocks; "Courier New" is universally present. */
export const MONO_FONT = 'Courier New'
/** Light grey fill behind code, matching a subtle LaTeX `verbatim` block. */
export const CODE_FILL = 'F2F2F2'

/** Numbering reference shared by all headings → continuous 1 / 1.1 / 1.1.1. */
export const HEADING_NUMBERING_REF = 'inqtrix-heading-numbering'
/** Numbering reference for ordered lists; each list gets its own `instance`. */
export const ORDERED_LIST_REF = 'inqtrix-ordered-list'
/** Numbering reference for bullet lists. */
export const BULLET_LIST_REF = 'inqtrix-bullet-list'

/** Body font size in half-points (22 = 11pt). */
export const BODY_SIZE = 22
/** Body line spacing in twentieths of a point (276 ≈ 1.15 lines). */
export const LINE_SPACING = 276

const BODY_FIRST_LINE_INDENT = convertMillimetersToTwip(6)

export const latexReportStyles: IStylesOptions = {
  default: {
    document: {
      run: { font: SERIF_FONT, size: BODY_SIZE },
      paragraph: {
        alignment: AlignmentType.JUSTIFIED,
        spacing: { line: LINE_SPACING, after: 0 },
        indent: { firstLine: BODY_FIRST_LINE_INDENT },
      },
    },
    title: {
      run: { font: SERIF_FONT, size: 40, bold: true },
      paragraph: { alignment: AlignmentType.CENTER, spacing: { after: 120 }, indent: { firstLine: 0 } },
    },
    heading1: {
      run: { font: SERIF_FONT, size: 32, bold: true, color: '000000' },
      paragraph: { alignment: AlignmentType.LEFT, spacing: { before: 360, after: 140 }, indent: { firstLine: 0 }, keepNext: true },
    },
    heading2: {
      run: { font: SERIF_FONT, size: 28, bold: true, color: '000000' },
      paragraph: { alignment: AlignmentType.LEFT, spacing: { before: 300, after: 120 }, indent: { firstLine: 0 }, keepNext: true },
    },
    heading3: {
      run: { font: SERIF_FONT, size: 24, bold: true, color: '000000' },
      paragraph: { alignment: AlignmentType.LEFT, spacing: { before: 260, after: 100 }, indent: { firstLine: 0 }, keepNext: true },
    },
    heading4: {
      run: { font: SERIF_FONT, size: BODY_SIZE, bold: true, italics: true, color: '000000' },
      paragraph: { alignment: AlignmentType.LEFT, spacing: { before: 220, after: 80 }, indent: { firstLine: 0 }, keepNext: true },
    },
  },
  paragraphStyles: [
    {
      id: 'InqtrixSubtitle',
      name: 'Inqtrix Subtitle',
      basedOn: 'Normal',
      next: 'Normal',
      run: { font: SERIF_FONT, size: 24, color: '444444' },
      paragraph: { alignment: AlignmentType.CENTER, spacing: { after: 280 }, indent: { firstLine: 0 } },
    },
    {
      id: 'InqtrixCode',
      name: 'Inqtrix Code',
      basedOn: 'Normal',
      next: 'InqtrixCode',
      run: { font: MONO_FONT, size: 20 },
      paragraph: {
        alignment: AlignmentType.LEFT,
        spacing: { line: 240, after: 0 },
        indent: { firstLine: 0, left: convertMillimetersToTwip(4) },
        shading: { type: ShadingType.CLEAR, color: 'auto', fill: CODE_FILL },
      },
    },
    {
      id: 'InqtrixQuote',
      name: 'Inqtrix Quote',
      basedOn: 'Normal',
      next: 'Normal',
      run: { font: SERIF_FONT, size: BODY_SIZE, italics: true, color: '444444' },
      paragraph: {
        alignment: AlignmentType.LEFT,
        spacing: { line: LINE_SPACING, before: 80, after: 80 },
        indent: { firstLine: 0, left: convertMillimetersToTwip(8) },
        border: { left: { style: BorderStyle.SINGLE, size: 18, space: 12, color: 'BBBBBB' } },
      },
    },
  ],
}

function decimalLevels() {
  return [0, 1, 2, 3].map((level) => ({
    level,
    format: LevelFormat.DECIMAL,
    text: `%${level + 1}.`,
    alignment: AlignmentType.START,
    style: {
      paragraph: {
        indent: {
          left: convertMillimetersToTwip(8 * (level + 1)),
          hanging: convertMillimetersToTwip(6),
        },
      },
    },
  }))
}

const BULLET_GLYPHS = ['•', '◦', '▪', '·']

export const latexReportNumbering: INumberingOptions = {
  config: [
    {
      reference: HEADING_NUMBERING_REF,
      levels: [0, 1, 2, 3].map((level) => ({
        level,
        format: LevelFormat.DECIMAL,
        text: Array.from({ length: level + 1 }, (_unused, index) => `%${index + 1}`).join('.'),
        alignment: AlignmentType.START,
        style: { paragraph: { indent: { left: 0, firstLine: 0 } } },
      })),
    },
    {
      reference: ORDERED_LIST_REF,
      levels: decimalLevels(),
    },
    {
      reference: BULLET_LIST_REF,
      levels: [0, 1, 2, 3].map((level) => ({
        level,
        format: LevelFormat.BULLET,
        text: BULLET_GLYPHS[level],
        alignment: AlignmentType.START,
        style: {
          paragraph: {
            indent: {
              left: convertMillimetersToTwip(8 * (level + 1)),
              hanging: convertMillimetersToTwip(6),
            },
          },
        },
      })),
    },
  ],
}

/**
 * Build the single section: A4 page, ~25 mm margins, and a centered page
 * number in the footer.
 *
 * Args:
 *   children: the already-built body content (title block + walked markdown).
 *   footerNotice: optional line placed above the page number on every page.
 *     Used for the AI-generation disclosure on exports of model-generated
 *     content. Repeating it per page keeps the marker attached when a single
 *     page is printed or lifted out of the document. Omitted for documents
 *     the user wrote themselves.
 */
export function latexReportSection(
  children: ISectionOptions['children'],
  footerNotice?: string,
): ISectionOptions {
  return {
    properties: {
      page: {
        size: {
          width: convertMillimetersToTwip(210),
          height: convertMillimetersToTwip(297),
        },
        margin: {
          top: convertMillimetersToTwip(25),
          bottom: convertMillimetersToTwip(25),
          left: convertMillimetersToTwip(25),
          right: convertMillimetersToTwip(25),
        },
      },
    },
    footers: {
      default: new Footer({
        children: [
          ...(footerNotice?.trim()
            ? [new Paragraph({
              alignment: AlignmentType.CENTER,
              indent: { firstLine: 0 },
              children: [new TextRun({
                font: SERIF_FONT,
                size: 14,
                color: '666666',
                text: footerNotice.trim(),
              })],
            })]
            : []),
          new Paragraph({
            alignment: AlignmentType.CENTER,
            indent: { firstLine: 0 },
            children: [new TextRun({ font: SERIF_FONT, size: 18, children: [PageNumber.CURRENT] })],
          }),
        ],
      }),
    },
    children,
  }
}
