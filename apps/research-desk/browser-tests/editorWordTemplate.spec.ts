import { readFile } from 'node:fs/promises'

import { expect, test, type Page } from '@playwright/test'
import JSZip from 'jszip'

const fixturePath = '/browser-tests/fixtures/editor-lifecycle.html'
const XML = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
const WORD_NAMESPACE = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
const DOCX_TYPE = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'

async function template(body: string): Promise<Buffer> {
  const zip = new JSZip()
  zip.file('[Content_Types].xml', `${XML}<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="${DOCX_TYPE}.main+xml"/><Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/></Types>`)
  zip.file('_rels/.rels', `${XML}<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>`)
  zip.file('word/_rels/document.xml.rels', `${XML}<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>`)
  zip.file('word/styles.xml', `${XML}<w:styles ${WORD_NAMESPACE}><w:style w:type="paragraph" w:styleId="berschrift1"><w:name w:val="heading 1"/></w:style></w:styles>`)
  zip.file('word/document.xml', `${XML}<w:document ${WORD_NAMESPACE}><w:body>${body}<w:sectPr/></w:body></w:document>`)
  return zip.generateAsync({ type: 'nodebuffer' })
}

const paragraph = (text: string) => `<w:p><w:r><w:t>${text}</w:t></w:r></w:p>`

async function chooseTemplate(page: Page, name: string, buffer: Buffer) {
  await page.getByRole('button', { name: /^(Weitere Dokumentaktionen|More document actions)$/ }).click()
  const chooser = page.waitForEvent('filechooser')
  await page.getByRole('menuitem', { name: /^(Mit Word-Vorlage exportieren|Export with Word template)/ }).click()
  await (await chooser).setFiles({ buffer, mimeType: DOCX_TYPE, name })
}

async function downloadedDocument(download: Awaited<ReturnType<Page['waitForEvent']>>): Promise<string> {
  const file = download as unknown as { path: () => Promise<string>; suggestedFilename: () => string }
  expect(file.suggestedFilename()).toMatch(/\.docx$/)
  const zip = await JSZip.loadAsync(await readFile(await file.path()))
  return zip.file('word/document.xml')!.async('string')
}

test.beforeEach(async ({ page }) => {
  page.on('pageerror', (error) => {
    throw error
  })
  await page.goto(`${fixturePath}?scenario=share-menu-focus`)
  await expect(page.getByRole('button', { name: /^(Weitere Dokumentaktionen|More document actions)$/ })).toBeVisible()
})

const notice = (page: Page) => page.locator('[data-editor-word-template-notice]')

test('a chosen template comes back as a download with the document in it', async ({ page }) => {
  await expect(page.locator('[data-editor-word-template-input]')).toHaveAttribute('accept', '.docx,.dotx')
  const download = page.waitForEvent('download')
  await chooseTemplate(page, 'firma.docx', await template(
    `${paragraph('Deckblatt {{title}}')}${paragraph('{{content}}')}${paragraph('Anhang')}`,
  ))
  const written = await downloadedDocument(await download)
  expect(written).toContain('Deckblatt')
  expect(written).toContain('Anhang')
  expect(written).not.toContain('{{')
  expect(written.indexOf('Deckblatt')).toBeLessThan(written.indexOf('Anhang'))
  await expect(notice(page)).toHaveCount(0)
})

test('a template without a marker still exports and says what happened, until closed', async ({ page }) => {
  const download = page.waitForEvent('download')
  await chooseTemplate(page, 'brief.docx', await template(paragraph('Briefbogen')))
  expect(await downloadedDocument(await download)).toContain('Briefbogen')
  await expect(notice(page)).toBeVisible()
  await expect(notice(page)).toContainText('{{content}}')
  await notice(page).getByRole('button').click()
  await expect(notice(page)).toHaveCount(0)
})

test('a refused file gives no download, says why, and can be chosen again', async ({ page }) => {
  let downloads = 0
  page.on('download', () => {
    downloads += 1
  })
  const consoleErrors: string[] = []
  page.on('console', (message) => {
    if (message.type() === 'error') consoleErrors.push(message.text())
  })
  await chooseTemplate(page, 'alt.doc', Buffer.from('kein Word'))
  await expect(notice(page)).toBeVisible()
  await expect(notice(page)).toContainText('.docx')
  // Otherwise the browser reports no change when the same file is chosen again.
  await expect(page.locator('[data-editor-word-template-input]')).toHaveValue('')
  await notice(page).getByRole('button').click()
  await chooseTemplate(page, 'alt.doc', Buffer.from('kein Word'))
  await expect(notice(page)).toBeVisible()
  expect(downloads).toBe(0)
  // A file the user can replace is no fault of the application.
  expect(consoleErrors).toEqual([])
})

test('every menu item stays on one line, in both languages of the label', async ({ page }) => {
  await page.getByRole('button', { name: /^(Weitere Dokumentaktionen|More document actions)$/ }).click()
  const normal = await page.getByRole('menuitem', { exact: true, name: /^(Als Word exportieren|Export to Word)$/ }).boundingBox()
  const withTemplate = await page.getByRole('menuitem', { name: /^(Mit Word-Vorlage exportieren|Export with Word template)/ }).boundingBox()
  // Engines round heights differently; a second line would add about 20 pixels.
  expect(Math.abs((withTemplate?.height ?? 0) - (normal?.height ?? 0))).toBeLessThan(4)
})

test('the normal export stays beside it', async ({ page }) => {
  const download = page.waitForEvent('download')
  await page.getByRole('button', { name: /^(Weitere Dokumentaktionen|More document actions)$/ }).click()
  await page.getByRole('menuitem', { exact: true, name: /^(Als Word exportieren|Export to Word)$/ }).click()
  const written = await downloadedDocument(await download)
  expect(written).toContain('w:sectPr')
  await expect(notice(page)).toHaveCount(0)
})
