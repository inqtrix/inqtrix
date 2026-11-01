import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { dirname, join } from 'node:path'

import JSZip from 'jszip'
import { describe, expect, it } from 'vitest'
import { type Element as XmlElement, xml2js } from 'xml-js'
import { buildDocxFromTemplate, exportMarkdownToDocxTemplate, TEMPLATE_MAX_BYTES } from './docxTemplateExport'
import { TemplateExportError } from './templateExportMessages'

const NAMESPACES = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
  + 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
const XML = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
const MAIN_DOCUMENT = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml'
const MAIN_TEMPLATE = 'application/vnd.openxmlformats-officedocument.wordprocessingml.template.main+xml'
const MAIN_MACRO_TEMPLATE = 'application/vnd.ms-word.template.macroEnabledTemplate.main+xml'
const AT = new Date(2026, 9, 30, 12, 0, 0)

const paragraph = (text: string) => `<w:p><w:r><w:t xml:space="preserve">${text}</w:t></w:r></w:p>`
const marker = paragraph('{{content}}')

/** A German Word's styles: the ids differ from the names. */
const GERMAN_STYLES = [
  '<w:style w:type="paragraph" w:default="1" w:styleId="Standard"><w:name w:val="Normal"/></w:style>',
  '<w:style w:type="paragraph" w:styleId="berschrift1"><w:name w:val="heading 1"/><w:rPr><w:color w:val="112233"/></w:rPr></w:style>',
  '<w:style w:type="paragraph" w:styleId="berschrift2"><w:name w:val="heading 2"/></w:style>',
  '<w:style w:type="paragraph" w:styleId="Listenabsatz"><w:name w:val="List Paragraph"/></w:style>',
  '<w:style w:type="paragraph" w:styleId="Zitat"><w:name w:val="Quote"/></w:style>',
  '<w:style w:type="character" w:styleId="Hyperlink"><w:name w:val="Hyperlink"/></w:style>',
  '<w:style w:type="table" w:styleId="Tabellenraster"><w:name w:val="Table Grid"/></w:style>',
].join('')

type TemplateSpec = {
  body?: string
  styles?: string
  numbering?: string
  header?: string
  footer?: string
  mainType?: string
  contentTypes?: string
  withoutStyles?: boolean
  files?: Record<string, string>
}

async function buildTemplate(spec: TemplateSpec = {}): Promise<Uint8Array> {
  const zip = new JSZip()
  const overrides = [
    `<Override PartName="/word/document.xml" ContentType="${spec.mainType ?? MAIN_DOCUMENT}"/>`,
    '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>',
    ...(spec.numbering ? ['<Override PartName="/word/numbering.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml"/>'] : []),
    ...(spec.header ? ['<Override PartName="/word/header1.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.header+xml"/>'] : []),
    ...(spec.footer ? ['<Override PartName="/word/footer1.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml"/>'] : []),
  ].join('')
  zip.file('[Content_Types].xml', `${XML}<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>${spec.contentTypes ?? ''}${overrides}</Types>`)
  zip.file('_rels/.rels', `${XML}<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>`)
  const relationships = [
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>',
    ...(spec.numbering ? ['<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/numbering" Target="numbering.xml"/>'] : []),
    ...(spec.header ? ['<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/header" Target="header1.xml"/>'] : []),
    ...(spec.footer ? ['<Relationship Id="rId4" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/footer" Target="footer1.xml"/>'] : []),
  ].join('')
  zip.file('word/_rels/document.xml.rels', `${XML}<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">${relationships}</Relationships>`)
  if (!spec.withoutStyles) zip.file('word/styles.xml', `${XML}<w:styles ${NAMESPACES}>${spec.styles ?? GERMAN_STYLES}</w:styles>`)
  if (spec.numbering) zip.file('word/numbering.xml', `${XML}<w:numbering ${NAMESPACES}>${spec.numbering}</w:numbering>`)
  if (spec.header) zip.file('word/header1.xml', `${XML}<w:hdr ${NAMESPACES}>${spec.header}</w:hdr>`)
  if (spec.footer) zip.file('word/footer1.xml', `${XML}<w:ftr ${NAMESPACES}>${spec.footer}</w:ftr>`)
  const sectionReferences = [
    ...(spec.header ? ['<w:headerReference w:type="default" r:id="rId3"/>'] : []),
    ...(spec.footer ? ['<w:footerReference w:type="default" r:id="rId4"/>'] : []),
  ].join('')
  zip.file('word/document.xml', `${XML}<w:document ${NAMESPACES}><w:body>${spec.body ?? marker}<w:sectPr>${sectionReferences}<w:pgSz w:w="11906" w:h="16838"/></w:sectPr></w:body></w:document>`)
  for (const [name, content] of Object.entries(spec.files ?? {})) zip.file(name, content)
  return zip.generateAsync({ type: 'uint8array' })
}

type Built = {
  document: string
  part: (name: string) => Promise<string>
  names: string[]
  notices: Awaited<ReturnType<typeof buildDocxFromTemplate>>['notices']
}

async function build(
  spec: TemplateSpec,
  markdown = 'Text.',
  title = 'Bericht',
  locale: 'de' | 'en' = 'de',
): Promise<Built> {
  const { bytes, notices } = await buildDocxFromTemplate(await buildTemplate(spec), markdown, title, { locale, now: AT })
  const zip = await JSZip.loadAsync(bytes)
  const part = async (name: string) => zip.file(name)!.async('string')
  return { document: await part('word/document.xml'), names: Object.keys(zip.files), notices, part }
}

async function refusal(spec: TemplateSpec | Uint8Array): Promise<string> {
  const bytes = spec instanceof Uint8Array ? spec : await buildTemplate(spec)
  const error = await buildDocxFromTemplate(bytes, 'Text.', 'Bericht', { locale: 'de', now: AT }).catch((reason: unknown) => reason)
  expect(error).toBeInstanceOf(TemplateExportError)
  return (error as TemplateExportError).code
}

function bodyOf(document: string): XmlElement[] {
  const root = xml2js(document, { compact: false }) as XmlElement
  const documentElement = root.elements!.find((element) => element.name === 'w:document')!
  return documentElement.elements!.find((element) => element.name === 'w:body')!.elements!
}

function textOf(element: XmlElement): string {
  if (element.type === 'text') return String(element.text)
  return (element.elements ?? []).map(textOf).join('')
}

function styleOf(element: XmlElement): string | undefined {
  const properties = element.elements?.find((child) => child.name === 'w:pPr')
  const style = properties?.elements?.find((child) => child.name === 'w:pStyle')
  return style?.attributes?.['w:val'] as string | undefined
}

function paragraphWithText(document: string, text: string): XmlElement {
  const found = bodyOf(document).find((element) => textOf(element).includes(text))
  expect(found, `no block holds "${text}"`).toBeDefined()
  return found!
}

/** Word refuses a part that uses, or asks to ignore, a prefix it does not declare. */
function undeclaredPrefixes(xml: string): string[] {
  const root = xml2js(xml, { compact: false }) as XmlElement
  const found = new Set<string>()
  const visit = (element: XmlElement, declared: Set<string>) => {
    const attributes = Object.keys(element.attributes ?? {})
    const inScope = new Set([
      ...declared,
      ...attributes.filter((name) => name.startsWith('xmlns:')).map((name) => name.slice(6)),
    ])
    const used = [element.name, ...attributes.filter((name) => !name.startsWith('xmlns'))]
      .filter((name): name is string => Boolean(name?.includes(':')))
      .map((name) => name.split(':')[0])
      .filter((prefix) => prefix !== 'xml')
    const ignorable = String(element.attributes?.['mc:Ignorable'] ?? '').split(/\s+/).filter(Boolean)
    for (const prefix of [...used, ...ignorable]) {
      if (!inScope.has(prefix)) found.add(prefix)
    }
    for (const child of element.elements ?? []) visit(child, inScope)
  }
  visit(root, new Set())
  return [...found]
}

describe('buildDocxFromTemplate: where the report goes', () => {
  it('puts the report at the marker, between the template text around it', async () => {
    const { document, notices } = await build(
      { body: `${paragraph('Deckblatt')}${marker}${paragraph('Anhang')}` },
      '# Kapitel\n\nAbsatz.',
    )
    expect(bodyOf(document).map(textOf).filter(Boolean)).toEqual(['Deckblatt', 'Kapitel', 'Absatz.', 'Anhang'])
    expect(document).not.toContain('{{content}}')
    expect(notices).toEqual([])
  })

  it('finds a marker Word split over runs, with proofing marks and a bookmark between them', async () => {
    const split = '<w:p><w:r><w:t>{{con</w:t></w:r><w:proofErr w:type="spellStart"/><w:bookmarkStart w:id="1" w:name="x"/>'
      + '<w:r><w:rPr><w:b/></w:rPr><w:t>tent</w:t></w:r><w:bookmarkEnd w:id="1"/><w:proofErr w:type="spellEnd"/><w:r><w:t>}}</w:t></w:r></w:p>'
    const { document, notices } = await build({ body: split }, 'Bericht.')
    expect(bodyOf(document).map(textOf).filter(Boolean)).toEqual(['Bericht.'])
    expect(notices).toEqual([])
  })

  it('finds a marker inside a tracked insertion, which the library alone would miss', async () => {
    const tracked = '<w:p><w:ins w:id="1" w:author="A" w:date="2026-01-01T00:00:00Z"><w:r><w:t>{{content}}</w:t></w:r></w:ins></w:p>'
    const { document } = await build({ body: tracked }, 'Bericht.')
    expect(bodyOf(document).map(textOf).filter(Boolean)).toEqual(['Bericht.'])
    expect(document).not.toContain('{{content}}')
  })

  it('removes a content control holding only the marker, which could break it otherwise', async () => {
    const control = `<w:sdt><w:sdtPr><w:text/></w:sdtPr><w:sdtContent>${marker}</w:sdtContent></w:sdt>`
    const { document } = await build({ body: control }, '# A\n\nB.')
    expect(document).not.toContain('w:sdt')
    expect(bodyOf(document).map(textOf).filter(Boolean)).toEqual(['A', 'B.'])
  })

  it('keeps a content control that holds more than the marker', async () => {
    const control = `<w:sdt><w:sdtContent>${paragraph('Davor')}${marker}</w:sdtContent></w:sdt>`
    const { document } = await build({ body: control }, 'B.')
    expect(document).toContain('w:sdtContent')
    expect(textOf(bodyOf(document)[0])).toBe('DavorB.')
  })

  it('keeps the section break Word stores in the marker paragraph', async () => {
    const lastOfSection = '<w:p><w:pPr><w:sectPr><w:pgSz w:w="16838" w:h="11906" w:orient="landscape"/></w:sectPr></w:pPr>'
      + '<w:r><w:t>{{content}}</w:t></w:r></w:p>'
    const { document } = await build({ body: `${lastOfSection}${paragraph('Anhang')}` }, 'Bericht.')
    expect(document.match(/w:orient="landscape"/g)).toHaveLength(1)
    const body = bodyOf(document)
    const carrier = body.findIndex((element) => JSON.stringify(element).includes('landscape'))
    expect(body.map(textOf).slice(0, 3)).toEqual(['Bericht.', '', 'Anhang'])
    expect(carrier).toBe(1)
  })

  it('ends a table cell with a paragraph when the report ends with a table', async () => {
    const cell = `<w:tbl><w:tblGrid><w:gridCol w:w="100"/></w:tblGrid><w:tr><w:tc>${marker}</w:tc></w:tr></w:tbl>`
    const { document } = await build({ body: cell }, '| A |\n| - |\n| 1 |')
    const outerCell = bodyOf(document)[0].elements!.find((element) => element.name === 'w:tr')!.elements![0]
    expect(outerCell.elements!.map((element) => element.name)).toEqual(['w:tbl', 'w:p'])
  })

  it('appends the report at the end and says so when the template has no marker', async () => {
    const { document, notices } = await build({ body: paragraph('Briefbogen') }, 'Bericht.')
    const body = bodyOf(document)
    expect(body.map(textOf).filter(Boolean)).toEqual(['Briefbogen', 'Bericht.'])
    expect(body[body.length - 1].name).toBe('w:sectPr')
    expect(notices).toEqual([{ code: 'content_marker_missing' }])
  })

  it('takes an empty document without losing the place of the marker', async () => {
    const { document, notices } = await build({ body: `${paragraph('Deckblatt')}${marker}` }, '')
    expect(bodyOf(document).map((element) => element.name)).toEqual(['w:p', 'w:p', 'w:sectPr'])
    expect(document).not.toContain('{{')
    expect(notices).toEqual([])
  })

  it('refuses a marker in a header or a footer, where the report would land', async () => {
    expect(await refusal({ header: marker })).toBe('content_marker_outside_body')
    expect(await refusal({ footer: marker })).toBe('content_marker_outside_body')
  })

  it('refuses a marker that occurs twice', async () => {
    expect(await refusal({ body: `${marker}${marker}` })).toBe('content_marker_repeated')
    expect(await refusal({ body: paragraph('{{content}} {{content}}') })).toBe('content_marker_repeated')
  })

  it('refuses a marker beside anything the replacement would destroy', async () => {
    expect(await refusal({ body: paragraph('Hier: {{content}}') })).toBe('content_marker_not_alone')
    const beside = '<w:p><w:r><w:drawing/></w:r><w:r><w:t>{{content}}</w:t></w:r></w:p>'
    expect(await refusal({ body: beside })).toBe('content_marker_not_alone')
  })

  it('takes spaces around the marker as alone', async () => {
    const { document } = await build({ body: paragraph(' {{content}} ') }, 'Bericht.')
    expect(bodyOf(document).map(textOf).filter(Boolean)).toEqual(['Bericht.'])
  })
})

describe('buildDocxFromTemplate: title and date', () => {
  it('replaces them in the text, the header and the footer, in the format around them', async () => {
    const styled = '<w:p><w:r><w:rPr><w:b/></w:rPr><w:t>{{title}}</w:t></w:r></w:p>'
    const built = await build({
      body: `${styled}${paragraph('Stand {{date}}')}${marker}`,
      header: paragraph('Kopf {{title}}'),
      footer: paragraph('Fuß {{date}}'),
    }, 'Text.', 'Quartalsbericht')
    const titled = paragraphWithText(built.document, 'Quartalsbericht')
    expect(textOf(titled)).toBe('Quartalsbericht')
    const run = titled.elements!.find((element) => element.name === 'w:r' && textOf(element) === 'Quartalsbericht')!
    expect(run.elements!.find((element) => element.name === 'w:rPr')?.elements?.map(({ name }) => name)).toContain('w:b')
    expect(textOf(paragraphWithText(built.document, 'Stand'))).toBe('Stand 30.10.2026')
    expect(await built.part('word/header1.xml')).toContain('Quartalsbericht')
    expect(await built.part('word/footer1.xml')).toContain('30.10.2026')
    expect(built.document).not.toContain('{{')
  })

  it('writes the date in the language of the interface', async () => {
    const { document } = await build({ body: `${paragraph('{{date}}')}${marker}` }, 'Text.', 'T', 'en')
    expect(document).toContain('10/30/2026')
  })

  it('replaces a marker that occurs twice in one paragraph', async () => {
    const { document } = await build({ body: `${paragraph('{{title}} und {{title}}')}${marker}` }, 'Text.', 'T')
    expect(textOf(paragraphWithText(document, 'und'))).toBe('T und T')
  })

  it('writes a title with XML characters as text', async () => {
    const { document } = await build({ body: `${paragraph('{{title}}')}${marker}` }, 'Text.', 'A & B <c> "d"')
    expect(document).toContain('A &amp; B &lt;c&gt; "d"')
  })

  it('names an empty title like the normal export', async () => {
    const { document } = await build({ body: `${paragraph('{{title}}')}${marker}` }, 'Text.', '   ')
    expect(textOf(paragraphWithText(document, 'Dokument'))).toBe('Dokument')
  })

  it('leaves the markers in the text of the report alone', async () => {
    const report = 'Ein Muster: {{title}}, {{date}} und {{content}}.'
    const { document } = await build({ body: `${paragraph('{{title}}')}${paragraph('{{date}}')}${marker}` }, report, 'Titel')
    expect(textOf(paragraphWithText(document, 'Ein Muster'))).toBe(report)
    expect(bodyOf(document).map(textOf).filter(Boolean)).toEqual(['Titel', '30.10.2026', report])
  })

  it('does not take a title holding a marker for the marker', async () => {
    const title = 'Über {{content}} und {{title}} und {{date}}'
    const { document } = await build({ body: `${paragraph('{{title}}')}${marker}` }, 'Bericht.', title)
    expect(bodyOf(document).map(textOf).filter(Boolean)).toEqual([title, 'Bericht.'])
  })

  it('says which marker stays visible when the library cannot reach it', async () => {
    const tracked = '<w:p><w:ins w:id="1" w:author="A" w:date="2026-01-01T00:00:00Z"><w:r><w:t>{{title}}</w:t></w:r></w:ins></w:p>'
    const inControl = '<w:p><w:sdt><w:sdtContent><w:r><w:t>{{date}}</w:t></w:r></w:sdtContent></w:sdt></w:p>'
    const { notices } = await build({ body: `${tracked}${inControl}${marker}` })
    expect(notices).toEqual([{ code: 'markers_not_replaced', markers: ['{{date}}', '{{title}}'] }])
  })

  it('has nothing to say about a marker it replaced', async () => {
    const { notices } = await build({ body: `${paragraph('{{title}}')}${paragraph('{{date}}')}${marker}` })
    expect(notices).toEqual([])
  })
})

describe('buildDocxFromTemplate: the template stays', () => {
  it('keeps the section, its header and footer, and the template text as written', async () => {
    const { document, part, names } = await build({
      body: `${paragraph('A &amp;amp; B')}${marker}`,
      header: paragraph('Kopf'),
      footer: paragraph('Fuß'),
      files: { 'word/media/logo.bin': 'BYTES', 'customXml/item1.xml': `${XML}<x>1</x>` },
    }, 'Bericht.')
    expect(document).toContain('A &amp;amp; B')
    expect(document).toContain('w:headerReference')
    expect(document).toContain('w:footerReference')
    expect(await part('word/header1.xml')).toContain('Kopf')
    expect(await part('word/media/logo.bin')).toBe('BYTES')
    expect(names).toContain('customXml/item1.xml')
  })

  it('keeps an attribute value with special characters through the rewrite', async () => {
    const value = 'A&amp;B &quot;x&quot; &lt;y&gt; &apos;z&apos;'
    const quoted = `<w:p><w:r><w:rPr><w:rFonts w:ascii="${value}"/></w:rPr><w:t>Rest</w:t></w:r></w:p>`
    const { document } = await build({ body: `${quoted}${marker}` })
    expect(document).toContain(`w:ascii="${value}"`)
  })
})

describe('buildDocxFromTemplate: what Word needs to open the file', () => {
  const report = '# Eins\n\n- A\n\n1. B\n\n| A |\n| - |\n| 1 |\n\n[L](https://example.com)'

  it('declares every prefix in the parts it writes, also beside the prefixes of a real template', async () => {
    expect(undeclaredPrefixes('<w:a xmlns:mc="m" mc:Ignorable="w9"/>')).toEqual(['w', 'w9'])
    const built = await build({}, report)
    for (const name of ['word/document.xml', 'word/numbering.xml', 'word/styles.xml']) {
      expect(undeclaredPrefixes(await built.part(name)), name).toEqual([])
    }
  })

  it('writes no empty file named like a folder, whether the template has folder entries or not', async () => {
    const withFolders = await buildTemplate({ header: paragraph('{{title}}') })
    const withoutFolders = new JSZip()
    for (const [name, entry] of Object.entries((await JSZip.loadAsync(withFolders)).files)) {
      if (!entry.dir) withoutFolders.file(name, await entry.async('uint8array'), { createFolders: false })
    }
    for (const template of [withFolders, await withoutFolders.generateAsync({ type: 'uint8array' })]) {
      const { bytes } = await buildDocxFromTemplate(template, report, 'T', { locale: 'de', now: AT })
      const written = await JSZip.loadAsync(bytes)
      const folders = Object.values(written.files).filter(({ name }) => name.endsWith('/'))
      expect(folders.length).toBeGreaterThan(0)
      // The directory bit of the zip entry, which the name alone does not set.
      expect(folders.map(({ dosPermissions }) => (dosPermissions ?? 0) & 0x10)).toEqual(folders.map(() => 0x10))
    }
  })

  it('writes the blocks of a table in the order the file format asks for', async () => {
    const { document } = await build({}, report)
    const properties = document.match(/<w:tblPr>.*?<\/w:tblPr>/)![0]
    expect([...properties.matchAll(/<(w:\w+)/g)].map((match) => match[1]))
      .toEqual(['w:tblPr', 'w:tblStyle', 'w:tblW', 'w:tblLook'])
    expect(document.indexOf('<w:tblGrid>')).toBeGreaterThan(document.indexOf('</w:tblPr>'))
  })
})

describe('buildDocxFromTemplate: files that Word itself wrote', () => {
  const wordFiles = join(dirname(createRequire(import.meta.url).resolve('mammoth/package.json')), 'test/test-data')
  const report = '# Eins\n\n- A\n\n1. B\n\n| A |\n| - |\n| 1 |\n\n[L](https://example.com)\n\nSchluss.'

  it.each(['comments', 'footnotes', 'simple-list', 'tables', 'text-box', 'tiny-picture'])(
    '%s.docx takes the report and keeps every part it had',
    async (name) => {
      const template = new Uint8Array(readFileSync(join(wordFiles, `${name}.docx`)))
      const { bytes } = await buildDocxFromTemplate(template, report, 'T', { locale: 'de', now: AT })
      const written = await JSZip.loadAsync(bytes)
      const writtenNames = Object.keys(written.files)
      for (const part of Object.keys((await JSZip.loadAsync(template)).files)) {
        expect(writtenNames).toContain(part)
      }
      const document = await written.file('word/document.xml')!.async('string')
      const body = bodyOf(document)
      expect(textOf(body[body.length - 2])).toBe('Schluss.')
      expect(body[body.length - 1].name).toBe('w:sectPr')
      for (const part of ['word/document.xml', 'word/numbering.xml', 'word/styles.xml']) {
        expect(undeclaredPrefixes(await written.file(part)!.async('string')), part).toEqual([])
      }
      const numbering = await written.file('word/numbering.xml')!.async('string')
      expect(numbering.lastIndexOf('<w:abstractNum ')).toBeLessThan(numbering.indexOf('<w:num '))
    },
  )

  it('refuses a file in the strict format, whose namespaces do not mix with what is added', async () => {
    expect(await refusal(new Uint8Array(readFileSync(join(wordFiles, 'strict-format.docx'))))).toBe('template_is_strict')
  })
})

describe('buildDocxFromTemplate: the look comes from the template', () => {
  it('uses the template styles by their name, whatever their id', async () => {
    const markdown = '# Eins\n\n## Zwei\n\n- Punkt\n\n> Zitat\n\n[Link](https://example.com)'
    const { document, notices } = await build({}, markdown)
    expect(styleOf(paragraphWithText(document, 'Eins'))).toBe('berschrift1')
    expect(styleOf(paragraphWithText(document, 'Zwei'))).toBe('berschrift2')
    expect(styleOf(paragraphWithText(document, 'Punkt'))).toBe('Listenabsatz')
    expect(styleOf(paragraphWithText(document, 'Zitat'))).toBe('Zitat')
    expect(document).toContain('<w:rStyle w:val="Hyperlink"/>')
    expect(document).not.toContain('Heading1')
    expect(document).not.toContain('Inqtrix')
    expect(notices).toEqual([])
  })

  it('leaves the template styles untouched and sets no report look', async () => {
    const built = await build({}, '# Eins\n\nText.')
    expect(await built.part('word/styles.xml')).toContain('<w:color w:val="112233"/>')
    expect(await built.part('word/styles.xml')).not.toContain('Times New Roman')
    expect(JSON.stringify(paragraphWithText(built.document, 'Text.'))).not.toContain('w:jc')
    expect(JSON.stringify(paragraphWithText(built.document, 'Eins'))).not.toContain('w:numPr')
  })

  it('takes style names in any case', async () => {
    const styles = '<w:style w:type="paragraph" w:styleId="H1"><w:name w:val="Heading 1"/></w:style>'
    const { document, notices } = await build({ styles }, '# Eins')
    expect(styleOf(paragraphWithText(document, 'Eins'))).toBe('H1')
    expect(notices).toEqual([])
  })

  it('borrows the nearest heading for a missing level, keeps its outline level and says so', async () => {
    const { document, notices } = await build({}, '# Eins\n\n### Drei\n\n#### Vier')
    const three = paragraphWithText(document, 'Drei')
    expect(styleOf(three)).toBe('berschrift2')
    expect(JSON.stringify(three)).toContain('"w:outlineLvl","attributes":{"w:val":"2"}')
    expect(notices).toEqual([{ code: 'heading_levels_borrowed', levels: [3, 4] }])
  })

  it('borrows upward when no lower level exists', async () => {
    const styles = '<w:style w:type="paragraph" w:styleId="H2"><w:name w:val="heading 2"/></w:style>'
    const { document } = await build({ styles }, '# Eins')
    expect(styleOf(paragraphWithText(document, 'Eins'))).toBe('H2')
  })

  it('says nothing about a level the document does not use', async () => {
    const { notices } = await build({}, '# Eins\n\n## Zwei')
    expect(notices).toEqual([])
  })

  it('sets headings bold and larger with their outline level when the template has no heading style', async () => {
    const { document, notices } = await build({ styles: '' }, '## Zwei')
    const heading = JSON.stringify(paragraphWithText(document, 'Zwei'))
    expect(heading).toContain('"name":"w:b"')
    expect(heading).toContain('"w:sz","attributes":{"w:val":"28"}')
    expect(heading).toContain('"w:outlineLvl","attributes":{"w:val":"1"}')
    expect(notices).toEqual([{ code: 'heading_styles_missing' }])
  })

  it('falls back to direct formatting for code, quotes and links', async () => {
    const { document } = await build({ styles: '' }, '```\ncode\n```\n\n> Zitat\n\n[Link](https://example.com)')
    expect(JSON.stringify(paragraphWithText(document, 'code'))).toContain('Courier New')
    expect(JSON.stringify(paragraphWithText(document, 'Zitat'))).toContain('w:ind')
    expect(document).toContain('w:color w:val="0563C1"')
  })

  it('uses an Inqtrix Code style for code blocks when the template brings one', async () => {
    const styles = '<w:style w:type="paragraph" w:styleId="MyCode"><w:name w:val="Inqtrix Code"/></w:style>'
    const { document } = await build({ styles }, '```\ncode\n```')
    const line = paragraphWithText(document, 'code')
    expect(styleOf(line)).toBe('MyCode')
    expect(JSON.stringify(line)).not.toContain('Courier New')
  })
})

describe('buildDocxFromTemplate: lists', () => {
  it('adds the numbering part, its type and its relation to a template without one', async () => {
    const built = await build({}, '- A\n- B\n\n1. C')
    expect(built.names).toContain('word/numbering.xml')
    expect(await built.part('[Content_Types].xml')).toContain('/word/numbering.xml')
    expect(await built.part('word/_rels/document.xml.rels')).toContain('numbering.xml')
  })

  it('keeps the numbering of the template, also on its headings, and adds its own above it', async () => {
    const numbering = '<w:abstractNum w:abstractNumId="7"><w:lvl w:ilvl="0"><w:pStyle w:val="berschrift1"/></w:lvl></w:abstractNum>'
      + '<w:num w:numId="9"><w:abstractNumId w:val="7"/></w:num>'
    const built = await build({ numbering }, '# Eins\n\n- A\n\n1. B')
    const written = await built.part('word/numbering.xml')
    expect(written).toContain('w:abstractNumId="7"')
    expect(written).toContain('<w:num w:numId="9"')
    const ids = [...built.document.matchAll(/<w:numId w:val="(\d+)"/g)].map((match) => Number(match[1]))
    expect(ids.length).toBe(2)
    expect(Math.min(...ids)).toBeGreaterThan(9)
    // Word refuses a file whose w:abstractNum comes after a w:num.
    expect(written.lastIndexOf('<w:abstractNum ')).toBeLessThan(written.indexOf('<w:num '))
    expect(JSON.stringify(paragraphWithText(built.document, 'Eins'))).not.toContain('w:numPr')
  })

  it('starts every ordered list on its own, also one inside a quote', async () => {
    const { document } = await build({}, '1. A\n\nText\n\n1. B\n\n> 1. C')
    const ids = [...document.matchAll(/<w:numId w:val="(\d+)"/g)].map((match) => match[1])
    expect(ids).toHaveLength(3)
    expect(new Set(ids).size).toBe(3)
  })

  it('gives a list a defined style in a template without List Paragraph', async () => {
    const styles = '<w:style w:type="paragraph" w:default="1" w:styleId="Standard"><w:name w:val="Normal"/></w:style>'
    const built = await build({ styles }, '- A')
    const style = styleOf(paragraphWithText(built.document, 'A'))
    expect(style).toBeDefined()
    expect(await built.part('word/styles.xml')).toContain(`w:styleId="${style}"`)
  })
})

describe('buildDocxFromTemplate: tables', () => {
  const table = '| Kopf | B |\n| - | - |\n| 1 | 2 |'

  it('uses the table style without direct borders, which would win over it', async () => {
    const { document } = await build({}, table)
    expect(document).toContain('<w:tblStyle w:val="Tabellenraster"/>')
    expect(document).not.toContain('w:tblBorders')
    expect(document).not.toContain('w:tcBorders')
    expect(document).toContain('w:firstRow="true"')
    expect(document).toContain('w:tblHeader')
  })

  it('prefers a table style named Inqtrix Table', async () => {
    const styles = `${GERMAN_STYLES}<w:style w:type="table" w:styleId="Firma"><w:name w:val="Inqtrix Table"/></w:style>`
    const { document } = await build({ styles }, table)
    expect(document).toContain('<w:tblStyle w:val="Firma"/>')
  })

  it('keeps the header row bold and the template tables as they are', async () => {
    const own = '<w:tbl><w:tblPr><w:tblBorders><w:top w:val="double"/></w:tblBorders></w:tblPr><w:tblGrid/><w:tr><w:tc><w:p/></w:tc></w:tr></w:tbl>'
    const { document } = await build({ body: `${own}${marker}` }, table)
    expect(document).toContain('<w:top w:val="double"/>')
    expect(document.match(/w:tblBorders>/g)).toHaveLength(2)
    expect(JSON.stringify(paragraphWithText(document, 'Kopf'))).toContain('"name":"w:b"')
  })

  it('draws the table directly when the template has no table style', async () => {
    const { document } = await build({ styles: '' }, table)
    expect(document).not.toContain('w:tblStyle')
    expect(document).toContain('<w:top w:val="single" w:color="000000" w:sz="8"/>')
    expect(document).toContain('<w:insideH w:val="none"')
  })
})

describe('buildDocxFromTemplate: the file', () => {
  it('turns a template into a document, which Word needs for a .docx', async () => {
    const built = await build({ mainType: MAIN_TEMPLATE }, 'Bericht.')
    const types = await built.part('[Content_Types].xml')
    expect(types).toContain(MAIN_DOCUMENT)
    expect(types).not.toContain(MAIN_TEMPLATE)
    expect(built.document).toContain('Bericht.')
  })

  it('refuses macros, by the file type and by the macro part', async () => {
    expect(await refusal({ mainType: MAIN_MACRO_TEMPLATE })).toBe('template_has_macros')
    expect(await refusal({ files: { 'word/vbaProject.bin': 'x' } })).toBe('template_has_macros')
  })

  it('takes a template with a macro workbook embedded, whose macros are not the template\'s', async () => {
    const contentTypes = '<Default Extension="xlsm" ContentType="application/vnd.ms-excel.sheet.macroEnabled.12"/>'
    const { document } = await build({ contentTypes }, 'Bericht.')
    expect(document).toContain('Bericht.')
  })

  it('takes a template without a styles part', async () => {
    const { document, notices } = await build({ withoutStyles: true }, '# Eins\n\n- A')
    expect(textOf(paragraphWithText(document, 'Eins'))).toBe('Eins')
    expect(notices).toEqual([{ code: 'heading_styles_missing' }])
  })

  it('refuses what is not a Word file', async () => {
    expect(await refusal(new TextEncoder().encode('kein zip'))).toBe('template_unreadable')
    const zip = new JSZip()
    zip.file('[Content_Types].xml', '<Types/>')
    expect(await refusal(await zip.generateAsync({ type: 'uint8array' }))).toBe('template_unreadable')
  })

  it('refuses a template above the limit before reading it', async () => {
    const file = new File([new Uint8Array(TEMPLATE_MAX_BYTES + 1)], 'gross.docx')
    const error = await exportMarkdownToDocxTemplate(file, 'Text.', 'T', 'de').catch((reason: unknown) => reason)
    expect(error).toBeInstanceOf(TemplateExportError)
    expect((error as TemplateExportError).code).toBe('template_too_large')
  })
})
