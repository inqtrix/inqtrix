import { createEditorSchemaExtensions } from '@inqtrix/editor-schema'
import { getSchema, type JSONContent } from '@tiptap/core'
import { describe, expect, it } from 'vitest'

import { citationChipRanges, type CitationChips } from './citationChips'

const schema = getSchema(createEditorSchemaExtensions({ enableUndoRedo: false }))
const chips: CitationChips = {
  isLabel: (label) => /^[KWE]\d+$/.test(label),
  labels: new Set(['K1', 'K2', 'W7']),
  onOpen: () => undefined,
}

const doc = (...content: JSONContent[]): JSONContent => ({ type: 'doc', content })
const paragraph = (...content: JSONContent[]): JSONContent => ({ type: 'paragraph', content })
const text = (value: string, marks?: JSONContent['marks']): JSONContent => (
  marks ? { type: 'text', text: value, marks } : { type: 'text', text: value }
)

/** Each chip as `label:text under it`, in document order. */
function chipsIn(json: JSONContent): string[] {
  const node = schema.nodeFromJSON(json)
  return citationChipRanges(node, chips).map((range) => `${range.label}:${node.textBetween(range.from, range.to)}`)
}

describe('citation chips while editing', () => {
  it('mark a known label in a single and a grouped bracket and standing alone', () => {
    expect(chipsIn(doc(paragraph(text('Beleg [K1] und Gruppe [K2, W7], siehe auch K1.'))))).toEqual([
      'K1:K1',
      'K2:K2',
      'W7:W7',
      'K1:K1',
    ])
  })

  it('leave an unknown label, a marker, an unparsable and an escaped bracket as text', () => {
    expect(chipsIn(doc(paragraph(text('Neu [K9], [unbelegt: K2], [K1–K2], [siehe K1], wörtlich \\[K1].'))))).toEqual([])
  })

  it('leave code as text', () => {
    expect(chipsIn(doc(
      paragraph(text('Code '), text('[K1]', [{ type: 'code' }]), text(' und [W7].')),
      { type: 'codeBlock', content: [text('[K2]')] },
    ))).toEqual(['W7:W7'])
  })

  it('read a label across marks and after a hard break', () => {
    expect(chipsIn(doc(paragraph(
      text('['),
      text('K1', [{ type: 'bold' }]),
      text(']'),
      { type: 'hardBreak' },
      text('[K2]'),
    )))).toEqual(['K1:K1', 'K2:K2'])
  })

  it('mark nothing without known labels', () => {
    const node = schema.nodeFromJSON(doc(paragraph(text('Beleg [K1].'))))
    expect(citationChipRanges(node, { ...chips, labels: new Set() })).toEqual([])
  })
})
