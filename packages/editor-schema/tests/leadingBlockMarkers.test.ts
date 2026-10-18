import { getSchema } from '@tiptap/core'
import { describe, expect, it } from 'vitest'

import { createEditorSchemaExtensions } from '../src/extensions.js'
import { parseEditorMarkdown, serializeEditorJson } from '../src/markdown.js'

// Text a writer typed that begins like a Markdown block ("## ", "- ", "1. ",
// "> ") stays text: what the projection stores reads back as the same
// document, in every block and after a hard line break.

const schema = getSchema(createEditorSchemaExtensions({ enableUndoRedo: false }))

const MARKERS = [
  '# Titel im Text',
  '## Der Bereich teilt',
  '###### Sechs Rauten',
  '- Strich am Anfang',
  '* Stern am Anfang',
  '+ Plus am Anfang',
  '1. Bundesliga spielt',
  '1) Klammer nach der Zahl',
  '> Kein Zitat',
  '```nicht Code',
  '~~~ nicht Code',
  '---',
  '***',
  '___',
  '<div>kein HTML</div>',
  '#',
  '-',
  '1.',
  '===',
]

const CONTROLS = [
  '#ohne Leerzeichen',
  '-5 Grad im Schatten',
  '3.5 Prozent sind wenig',
  '2026 war ein Jahr',
  'Ein ganz normaler Satz.',
]

type Json = { type: string; text?: string; content?: Json[] }

const text = (value: string): Json => ({ type: 'text', text: value })
const paragraph = (...content: Json[]): Json => ({ type: 'paragraph', content })

const CONTEXTS: Array<[string, (value: string) => Json]> = [
  ['a paragraph', (value) => paragraph(text(value))],
  ['a list item', (value) => ({
    type: 'bulletList',
    content: [{ type: 'listItem', content: [paragraph(text(value))] }],
  })],
  ['a quote', (value) => ({ type: 'blockquote', content: [paragraph(text(value))] })],
  ['the line after a hard break', (value) => paragraph(text('Erste Zeile'), { type: 'hardBreak' }, text(value))],
]

function roundTrips(block: Json): { markdown: string; same: boolean } {
  const document = { type: 'doc', content: [paragraph(text('Davor.')), block] }
  const markdown = serializeEditorJson(document, 'final')
  const back = schema.nodeFromJSON(parseEditorMarkdown(markdown))
  return { markdown, same: back.eq(schema.nodeFromJSON(document)) }
}

describe('leading Markdown block markers in text', () => {
  for (const [context, build] of CONTEXTS) {
    it.each(MARKERS)(`stays text in ${context}: %s`, (marker) => {
      const { markdown, same } = roundTrips(build(marker))
      expect(same, markdown).toBe(true)
    })

    it.each(CONTROLS)(`leaves plain text in ${context} as it is: %s`, (value) => {
      const { markdown, same } = roundTrips(build(value))
      expect(same, markdown).toBe(true)
      expect(markdown, 'no escape where nothing reads as a block').not.toContain('\\')
    })
  }
})
