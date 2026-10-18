import { describe, expect, it } from 'vitest'
import type { JSONContent } from '@tiptap/core'
import { Fragment, Schema, Slice, type Node as ProseMirrorNode } from '@tiptap/pm/model'
import {
  improvedMentionText,
  instructionTextFromDoc,
  mentionDocFromText,
  mentionTextFromDoc,
  pillRefsFromDoc,
  plainPastedSlice,
  type LabelResolver,
} from './mentionDoc'

function pill(refId: string, refKind: string, refLabel: string): JSONContent {
  return { attrs: { refId, refKind, refLabel }, type: 'mentionPill' }
}

function paragraph(...content: JSONContent[]): JSONContent {
  return { content, type: 'paragraph' }
}

const TWO_PILLS: JSONContent = {
  content: [
    paragraph(
      { text: 'Compare ', type: 'text' },
      pill('f1', 'file-asset', 'alpha'),
      { text: ' with ', type: 'text' },
      pill('g1', 'file-group', 'beta'),
    ),
  ],
  type: 'doc',
}

describe('instructionTextFromDoc', () => {
  it('renders pills as [N] in reading order', () => {
    expect(instructionTextFromDoc(TWO_PILLS)).toBe('Compare [1] with [2]')
  })

  it('returns an empty string for an empty doc', () => {
    expect(instructionTextFromDoc({ content: [paragraph()], type: 'doc' })).toBe('')
  })
})

describe('mentionTextFromDoc', () => {
  it('renders pills as @kind:label for the round-trip', () => {
    expect(mentionTextFromDoc(TWO_PILLS)).toBe('Compare @files:alpha with @filegroups:beta')
  })
})

describe('pillRefsFromDoc', () => {
  it('extracts positional references in reading order', () => {
    expect(pillRefsFromDoc(TWO_PILLS)).toEqual([
      { fileId: 'f1', kind: 'file-asset' },
      { groupId: 'g1', kind: 'file-group' },
    ])
  })
})

describe('mentionDocFromText', () => {
  const resolve: LabelResolver = (kind, label) => {
    if (kind === 'file-asset' && label === 'alpha') return { id: 'f1', label: 'alpha' }
    if (kind === 'file-group' && label === 'beta') return { id: 'g1', label: 'beta' }
    return null
  }

  it('turns resolvable @kind:label tokens into pills and round-trips', () => {
    const doc = mentionDocFromText('Compare @files:alpha with @filegroups:beta', resolve)
    expect(pillRefsFromDoc(doc)).toEqual([
      { fileId: 'f1', kind: 'file-asset' },
      { groupId: 'g1', kind: 'file-group' },
    ])
    expect(mentionTextFromDoc(doc)).toBe('Compare @files:alpha with @filegroups:beta')
    expect(instructionTextFromDoc(doc)).toBe('Compare [1] with [2]')
  })

  it('leaves unresolvable tokens as plain text', () => {
    const doc = mentionDocFromText('Use @files:missing here', resolve)
    expect(pillRefsFromDoc(doc)).toEqual([])
    expect(mentionTextFromDoc(doc)).toBe('Use @files:missing here')
  })
})

describe('a token kept plain by a word joiner', () => {
  const resolveAll: LabelResolver = (_kind, label) => ({ id: `id-${label}`, label })
  const WORD_JOINER = /\u2060/g

  it('stays text in this and every later conversion, reading the same', () => {
    const text = 'Hängt @files:gehaltsliste-2026, @FileGroups:personal und @research:audit an.'
    const plain = 'Hängt @\u2060files:gehaltsliste-2026, @\u2060FileGroups:personal und @\u2060research:audit an.'

    const doc = mentionDocFromText(plain, resolveAll)
    expect(pillRefsFromDoc(doc)).toEqual([])
    // A restored draft converts the composer text again.
    const again = mentionDocFromText(mentionTextFromDoc(doc), resolveAll)
    expect(pillRefsFromDoc(again)).toEqual([])
    expect(instructionTextFromDoc(again).replace(WORD_JOINER, '')).toBe(text)
  })

  it('leaves text without a reference token as it is', () => {
    const text = 'a@b.de nennt @rules:ton, @ alle und files:x.'
    const doc = mentionDocFromText(text, resolveAll)

    expect(pillRefsFromDoc(doc)).toEqual([])
    expect(instructionTextFromDoc(doc)).toBe(text)
  })
})

describe('improvedMentionText', () => {
  const resolveAll: LabelResolver = (_kind, label) => ({ id: `id-${label}`, label })
  const WORD_JOINER = /\u2060/g

  it('lets a token of the improved text convert only if the improved text held it', () => {
    // Pasted, the last two tokens carry the word joiner that keeps them plain.
    const source = 'Vergleiche @files:quartalsbericht und @FileGroups:Team mit '
      + '@\u2060files:gehaltsliste-2026 und @\u2060files:quartalsbericht-intern.'
    // The model dropped the word joiner and wrote a token of its own.
    const improved = 'Vergleiche @files:quartalsbericht und @filegroups:team mit '
      + '@files:gehaltsliste-2026 und @files:quartalsbericht-intern; siehe @research:audit.'

    const doc = mentionDocFromText(improvedMentionText(improved, source), resolveAll)

    expect(pillRefsFromDoc(doc)).toEqual([
      { fileId: 'id-quartalsbericht', kind: 'file-asset' },
      { groupId: 'id-team', kind: 'file-group' },
    ])
    expect(instructionTextFromDoc(doc).replace(WORD_JOINER, '')).toBe(
      'Vergleiche [1] und [2] mit @files:gehaltsliste-2026 und '
      + '@files:quartalsbericht-intern; siehe @research:audit.',
    )
  })

  it('leaves an improvement that writes no new token as it is', () => {
    const source = 'Nutze @files:quartalsbericht.'
    const improved = 'Bitte nutze @files:quartalsbericht als Quelle.'
    expect(improvedMentionText(improved, source)).toBe(improved)
  })
})

describe('plainPastedSlice', () => {
  // The node and mark names the composer's editor uses; the slice is applied
  // to an empty composer and read back the way a restored draft reads it.
  const schema = new Schema({
    marks: { bold: {} },
    nodes: {
      doc: { content: 'paragraph+' },
      hardBreak: { group: 'inline', inline: true },
      mentionPill: {
        atom: true,
        attrs: { refId: { default: '' }, refKind: { default: 'file-asset' }, refLabel: { default: '' } },
        group: 'inline',
        inline: true,
      },
      paragraph: { content: 'inline*' },
      text: { group: 'inline' },
    },
  })
  const resolveAll: LabelResolver = (_kind, label) => ({ id: `id-${label}`, label })
  const WORD_JOINER = /\u2060/g
  const bold = schema.mark('bold')

  /** A line as a paste hands it to the composer: one open paragraph. */
  function pastedLine(...content: ProseMirrorNode[]): Slice {
    return new Slice(Fragment.from(schema.node('paragraph', null, content)), 1, 1)
  }

  /** The composer after the paste, converted again as a restored draft is. */
  function rereadAfterPaste(slice: Slice): JSONContent {
    const empty = schema.node('doc', null, [schema.node('paragraph')])
    const pasted = empty.replace(1, 1, slice)
    return mentionDocFromText(mentionTextFromDoc(pasted.toJSON() as JSONContent), resolveAll)
  }

  const noDrag = { dragging: null }

  it('keeps pasted tokens plain, reading the same, also when the composer is read again', () => {
    const text = 'Hängt @files:gehaltsliste-2026, @FileGroups:personal und @research:audit an.'
    const slice = plainPastedSlice(pastedLine(schema.text(text)), noDrag)

    // The word joiner follows each `@`.
    expect(slice.content.textBetween(0, slice.content.size)).toBe(
      'Hängt @\u2060files:gehaltsliste-2026, @\u2060FileGroups:personal und @\u2060research:audit an.',
    )
    const reread = rereadAfterPaste(slice)
    expect(pillRefsFromDoc(reread)).toEqual([])
    expect(instructionTextFromDoc(reread).replace(WORD_JOINER, '')).toBe(text)
  })

  it('finds a token across marks, the way the composer text reads it', () => {
    // Formatted text from a web page or the shared document: the conversion
    // reads the composer without marks, so these two nodes form one token.
    const slice = plainPastedSlice(pastedLine(
      schema.text('Siehe '),
      schema.text('@files', [bold]),
      schema.text(':quartalsbericht bis Freitag.'),
    ), noDrag)

    expect(pillRefsFromDoc(rereadAfterPaste(slice))).toEqual([])
    // Marks stay where they were.
    expect(slice.content.firstChild!.child(1).marks).toEqual([bold])
  })

  it('keeps a token plain that only forms once the composer formats its Markdown', () => {
    // Markdown from a plain-text source: after the paste, the composer turns
    // `**…**`, `_…_`, `~~…~~` and `` `…` `` into formatting and drops the
    // markers, so each of these reads as a token afterwards. The styled
    // letters in front take two code units each, as on social media pages.
    const text = '\u{1D40D}\u{1D401}: **@**files:gehaltsliste-2026, @`files`:alpha, @files:`beta`, '
      + '~~@~~research:audit und _@_filegroups:team.'
    const slice = plainPastedSlice(pastedLine(schema.text(text)), noDrag)
    const pasted = slice.content.textBetween(0, slice.content.size)

    expect(pasted).toBe(
      '\u{1D40D}\u{1D401}: **@\u2060**files:gehaltsliste-2026, @\u2060`files`:alpha, @\u2060files:`beta`, '
      + '~~@\u2060~~research:audit und _@\u2060_filegroups:team.',
    )
    // Without its markers, as the formatting leaves it, nothing converts.
    expect(pillRefsFromDoc(mentionDocFromText(pasted.replace(/[*_~`]/g, ''), resolveAll))).toEqual([])
    // Pasted again, plain text stays as it is.
    const again = plainPastedSlice(pastedLine(schema.text(pasted)), noDrag)
    expect(again.content.textBetween(0, again.content.size)).toBe(pasted)
  })

  it('pastes a pill copied inside the app as the reference it was', () => {
    const pill = schema.node('mentionPill', { refId: 'file-own', refKind: 'file-asset', refLabel: 'quartalsbericht' })
    const slice = plainPastedSlice(pastedLine(schema.text('Quelle: '), pill, schema.text(' und @files:alpha')), noDrag)

    expect(pillRefsFromDoc(rereadAfterPaste(slice))).toEqual([
      { fileId: 'id-quartalsbericht', kind: 'file-asset' },
    ])
    // The text after the pill is text, its word joiner after the `@`.
    expect(slice.content.firstChild!.lastChild!.text).toBe(' und @\u2060files:alpha')
  })

  it('leaves text that a drag moves inside the composer as it is', () => {
    // The text is already in the composer; the drop hands back the very
    // slice the drag started with.
    const moved = pastedLine(schema.text('Quelle: @files:quartalsbericht'))

    expect(plainPastedSlice(moved, { dragging: { move: true, slice: moved } })).toBe(moved)
    // The same content from anywhere else arrives as text.
    const copy = pastedLine(schema.text('Quelle: @files:quartalsbericht'))
    expect(pillRefsFromDoc(rereadAfterPaste(
      plainPastedSlice(copy, { dragging: { move: true, slice: moved } }),
    ))).toEqual([])
  })

  it('leaves text without a reference token as it is', () => {
    const text = 'a@b.de nennt @rules:ton, @ alle und files:x.'
    const slice = plainPastedSlice(pastedLine(schema.text(text), schema.node('hardBreak')), noDrag)
    expect(slice.content.textBetween(0, slice.content.size)).toBe(text)
  })
})
