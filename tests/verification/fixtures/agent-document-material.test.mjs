import assert from 'node:assert/strict'
import { test } from 'node:test'

import { ATTACHMENT_PAGES, buildPdf } from './agent-document-material.mjs'

test('the attachment is a well-formed PDF whose cross-reference table is exact', () => {
  const pdf = buildPdf(ATTACHMENT_PAGES)
  const text = pdf.toString('latin1')
  assert(text.startsWith('%PDF-1.4\n'))
  const startxref = Number(text.match(/startxref\n(\d+)\n%%EOF\n$/)?.[1])
  assert.equal(text.slice(startxref, startxref + 5), 'xref\n')
  const entries = [...text.slice(startxref).matchAll(/^(\d{10}) 00000 n $/gm)].map((match) => Number(match[1]))
  // Catalog, page tree, font, and one page plus one content stream per page.
  assert.equal(entries.length, 3 + 2 * ATTACHMENT_PAGES.length)
  entries.forEach((offset, index) => {
    assert.equal(text.slice(offset, offset + `${index + 1} 0 obj\n`.length), `${index + 1} 0 obj\n`)
  })
  assert(text.includes(`/Count ${ATTACHMENT_PAGES.length} >>`))
})

test('a page without text carries no text operator, the others keep their German characters', () => {
  const text = buildPdf(['Prüfung (Teil 1)', null]).toString('latin1')
  const streams = [...text.matchAll(/stream\n([\s\S]*?)\nendstream/g)].map((match) => match[1])
  assert.equal(streams.length, 2)
  assert(streams[0].includes('(Prüfung \\(Teil 1\\)) Tj'))
  assert(!streams[1].includes('BT') && !streams[1].includes('Tj'))
})
