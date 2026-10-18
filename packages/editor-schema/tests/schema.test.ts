import { getSchema, type JSONContent } from '@tiptap/core'
import type { Node as ProseMirrorNode } from '@tiptap/pm/model'
import { EditorState, TextSelection } from '@tiptap/pm/state'
import { findWrapping } from '@tiptap/pm/transform'
import { initProseMirrorDoc, ySyncPluginKey } from '@tiptap/y-tiptap'
import * as Y from 'yjs'
import { describe, expect, it } from 'vitest'
import {
  EDITOR_BLOCK_SUGGESTIONS_SUPPORTED,
  INQTRIX_STRUCTURE_COMMAND_META,
  INQTRIX_STRUCTURE_SUGGESTION_ATTR,
  UnsupportedSuggestionStructureError,
  createEditorSchemaExtensions,
  createRelativePositionAdapter,
  deserializeRelativePosition,
  editorCollaborationRoom,
  editorJsonToYDoc,
  editorSchemaDescriptor,
  editorYDocToJson,
  fingerprintEditorSchemaDescriptor,
  getEditorSchemaFingerprint,
  parseEditorCollaborationRoom,
  parseEditorMarkdown,
  projectFinalJson,
  projectOriginalJson,
  resolveStructureSuggestion,
  SUGGESTION_MARK_NAMES,
  serializeEditorJson,
  serializeRelativePosition,
  type SuggestionKind,
  suggestionDescriptors,
  transformToInqtrixSuggestionTransaction,
  validateCanonicalYjsV1Update,
  validateEditorYDoc,
  validateSuggestionYjsUpdate,
} from '../src/index'

function suggestionMarkKinds(document: ProseMirrorNode): string[] {
  const kinds = new Set<string>()
  document.descendants((node) => {
    for (const mark of node.marks) {
      if (SUGGESTION_MARK_NAMES.has(mark.type.name as SuggestionKind)) {
        kinds.add(mark.type.name)
      }
    }
  })
  return [...kinds].sort()
}

function suggestionDeclaredKinds(document: ProseMirrorNode): string[] {
  const kinds = new Set<string>()
  document.descendants((node) => {
    for (const mark of node.marks) {
      if (SUGGESTION_MARK_NAMES.has(mark.type.name as SuggestionKind)) {
        kinds.add(String(mark.attrs.kind))
      }
    }
  })
  return [...kinds].sort()
}

function firstParagraphXml(document: Y.Doc): Y.XmlElement {
  const paragraph = document.getXmlFragment('content').get(0)
  if (!(paragraph instanceof Y.XmlElement)) throw new Error('Expected a Yjs paragraph element')
  return paragraph
}

function firstParagraphXmlText(document: Y.Doc): Y.XmlText {
  const text = firstParagraphXml(document).get(0)
  if (!(text instanceof Y.XmlText)) throw new Error('Expected Yjs paragraph text')
  return text
}

/** Every textblock of the document in order, with its children: `text:` and
 * the text of a text node (`text:!` when it carries attributes), or the name
 * of an element. */
function textblockChildren(document: Y.Doc): Array<[string, string[]]> {
  const found: Array<[string, string[]]> = []
  const visit = (element: Y.XmlElement): void => {
    if (['codeBlock', 'heading', 'paragraph'].includes(element.nodeName)) {
      found.push([element.nodeName, element.toArray().map((child) => (
        child instanceof Y.XmlText
          ? `text:${Object.keys(child.getAttributes()).length > 0 ? '!' : ''}${child.toString()}`
          : (child as Y.XmlElement).nodeName
      ))])
      return
    }
    for (const child of element.toArray()) {
      if (child instanceof Y.XmlElement) visit(child)
    }
  }
  for (const child of document.getXmlFragment('content').toArray()) {
    if (child instanceof Y.XmlElement) visit(child)
  }
  return found
}

function cloneYDoc(document: Y.Doc): Y.Doc {
  const clone = new Y.Doc()
  Y.applyUpdate(clone, Y.encodeStateAsUpdate(document))
  return clone
}

describe('editor schema gate', () => {
  it('normalizes empty Markdown to one canonical empty paragraph', () => {
    const parsed = parseEditorMarkdown('')
    const document = editorJsonToYDoc(parsed)

    expect(parsed).toEqual({
      type: 'doc',
      content: [{
        attrs: { inqtrixStructureSuggestion: null, textAlign: null },
        type: 'paragraph',
      }],
    })
    expect(editorYDocToJson(document)).toEqual(parsed)
    expect(serializeEditorJson(parsed)).toBe('')
    document.destroy()
  })

  it('round-trips one table-and-mathematics document through Markdown and Yjs', () => {
    const markdown = [
      '# Findings',
      '',
      '| Metric | Value |',
      '| --- | ---: |',
      '| Energy | 42 |',
      '',
      'Inline $E = mc^2$ remains inline.',
      '',
      '$$',
      '\\int_0^1 x^2 \\, dx',
      '$$',
      '',
    ].join('\n')

    const first = parseEditorMarkdown(markdown)
    const yDocument = editorJsonToYDoc(first)
    const restored = editorYDocToJson(yDocument)
    const serialized = serializeEditorJson(restored)
    const second = parseEditorMarkdown(serialized)

    expect(restored).toEqual(first)
    expect(second).toEqual(first)
    expect(serialized).toMatch(/\| Energy \| 42\s+\|/)
    expect(serialized).toContain('$E = mc^2$')
  })

  it('round-trips canonical JSON through the configured Yjs fragment', () => {
    const content = parseEditorMarkdown('## Shared\n\nA **collaborative** paragraph.\n')
    const document = editorJsonToYDoc(content)
    const replica = new Y.Doc()
    Y.applyUpdate(replica, Y.encodeStateAsUpdate(document))

    expect(editorYDocToJson(document)).toEqual(content)
    expect(validateEditorYDoc(replica)).toEqual(content)
    expect([...document.share.keys()]).toEqual(['content'])
    expect([...replica.share.keys()]).toEqual(['content'])
  })

  it('accepts only fully consumed canonical Yjs V1 update bytes', () => {
    const document = editorJsonToYDoc(parseEditorMarkdown('Canonical update'))
    const update = Y.encodeStateAsUpdate(document)
    const suffixed = new Uint8Array(update.byteLength + 3)
    suffixed.set(update)
    suffixed.set([0xde, 0xad, 0xbe], update.byteLength)
    const v2 = Y.encodeStateAsUpdateV2(document)

    expect(validateCanonicalYjsV1Update(update)).toBe(update)
    expect(() => validateCanonicalYjsV1Update(suffixed)).toThrow(/fully consumed/)
    expect(() => validateCanonicalYjsV1Update(v2)).toThrow(/canonical Yjs V1/)

    document.destroy()
  })

  it.each([
    ['an invisible XmlElement attribute', (document: Y.Doc) => {
      firstParagraphXml(document).setAttribute('rogue', 'hidden')
    }],
    ['an invisible XmlText attribute', (document: Y.Doc) => {
      firstParagraphXmlText(document).setAttribute('rogue', 'hidden')
    }],
    ['an empty extra XmlText node', (document: Y.Doc) => {
      const paragraph = firstParagraphXml(document)
      paragraph.insert(paragraph.length, [new Y.XmlText()])
    }],
  ] as const)('rejects %s that ProseMirror JSON drops', (_label, mutate) => {
    const content = parseEditorMarkdown('Canonical content')
    const document = editorJsonToYDoc(content)
    mutate(document)

    expect(editorYDocToJson(document)).toEqual(content)
    expect(() => validateEditorYDoc(document)).toThrow(/not canonical/)
    document.destroy()
  })

  it('writes one empty text node into every textblock without content, and accepts it', () => {
    const content: JSONContent = {
      content: [
        { type: 'paragraph' },
        { attrs: { level: 2 }, type: 'heading' },
        { type: 'codeBlock' },
        {
          content: [{ content: [{ type: 'paragraph' }], type: 'listItem' }],
          type: 'bulletList',
        },
        {
          content: [{
            content: [{ content: [{ type: 'paragraph' }], type: 'tableCell' }],
            type: 'tableRow',
          }],
          type: 'table',
        },
        { content: [{ text: 'Text', type: 'text' }], type: 'paragraph' },
        { content: [{ type: 'hardBreak' }], type: 'paragraph' },
      ],
      type: 'doc',
    }
    // What the document reads as: the text nodes are invisible in it.
    const canonical = getSchema(createEditorSchemaExtensions({ enableUndoRedo: false }))
      .nodeFromJSON(content)
      .toJSON()
    const document = editorJsonToYDoc(content)

    // Each empty textblock's only child is one empty text node without
    // attributes; a textblock with text or an inline node gets none.
    expect(textblockChildren(document)).toEqual([
      ['paragraph', ['text:']],
      ['heading', ['text:']],
      ['codeBlock', ['text:']],
      ['paragraph', ['text:']],
      ['paragraph', ['text:']],
      ['paragraph', ['text:Text']],
      ['paragraph', ['hardBreak']],
    ])
    expect(editorYDocToJson(document)).toEqual(canonical)
    expect(validateEditorYDoc(document)).toEqual(canonical)
    document.destroy()
  })

  it('accepts an empty textblock without a text node, as documents written without it hold it', () => {
    const content = parseEditorMarkdown('')
    const document = editorJsonToYDoc(content)
    const paragraph = firstParagraphXml(document)
    paragraph.delete(0, paragraph.length)

    expect(paragraph.length).toBe(0)
    expect(editorYDocToJson(document)).toEqual(content)
    expect(validateEditorYDoc(document)).toEqual(content)
    document.destroy()
  })

  it('rejects attributes on the empty text node of an empty textblock', () => {
    const document = editorJsonToYDoc(parseEditorMarkdown(''))
    // The transformer's own text node is the block's only child: the refusal
    // below is about its attribute, not about a second child.
    expect(textblockChildren(document)).toEqual([['paragraph', ['text:']]])
    firstParagraphXmlText(document).setAttribute('rogue', 'hidden')

    expect(() => validateEditorYDoc(document)).toThrow(/not canonical/)
    document.destroy()
  })

  it.each([
    ['a second empty text node', (paragraph: Y.XmlElement) => {
      paragraph.insert(paragraph.length, [new Y.XmlText()])
    }],
    ['a text node with text after it', (paragraph: Y.XmlElement) => {
      const text = new Y.XmlText()
      paragraph.insert(paragraph.length, [text])
      text.insert(0, 'Beta')
    }],
    ['a text node with text before it', (paragraph: Y.XmlElement) => {
      const text = new Y.XmlText()
      paragraph.insert(0, [text])
      text.insert(0, 'Alpha')
    }],
  ] as const)('rejects %s beside the empty text node of an empty textblock', (_label, mutate) => {
    const document = editorJsonToYDoc(parseEditorMarkdown(''))
    mutate(firstParagraphXml(document))

    expect(firstParagraphXml(document).length).toBe(2)
    expect(() => validateEditorYDoc(document)).toThrow(/not canonical/)
    document.destroy()
  })

  // Only a textblock may hold the empty text node. In any other element it
  // is a child the schema does not know, although the document reads the
  // same with it.
  it.each([
    ['a horizontal rule', (root: Y.XmlFragment) => root.get(0)],
    ['a line break', (root: Y.XmlFragment) => (root.get(1) as Y.XmlElement).get(0)],
    ['a table row', (root: Y.XmlFragment) => (root.get(2) as Y.XmlElement).get(1)],
  ] as const)('rejects an empty text node as the only child of %s', (_label, pick) => {
    const content: JSONContent = {
      content: [
        { type: 'horizontalRule' },
        { content: [{ type: 'hardBreak' }], type: 'paragraph' },
        {
          content: [
            {
              content: [{ content: [{ type: 'paragraph' }], type: 'tableCell' }],
              type: 'tableRow',
            },
            { type: 'tableRow' },
          ],
          type: 'table',
        },
      ],
      type: 'doc',
    }
    const document = editorJsonToYDoc(content)
    const canonical = validateEditorYDoc(document)
    const element = pick(document.getXmlFragment('content'))
    if (!(element instanceof Y.XmlElement)) throw new Error('fixture has no such element')
    expect(element.length).toBe(0)
    element.insert(0, [new Y.XmlText()])

    // The document still reads the same: only the structural comparison
    // can refuse it.
    expect(editorYDocToJson(document)).toEqual(canonical)
    expect(() => validateEditorYDoc(document)).toThrow(/not canonical/)
    document.destroy()
  })

  // Text alignment is rendered into a style attribute. Only the alignments
  // the editor offers may reach it; anything else would inject style text
  // (for example `display: none`, hiding a paragraph from every reader).
  it.each([
    ['style that hides the block', 'left; display: none'],
    ['a second declaration', 'center;color:red'],
    ['a very long value', 'x'.repeat(4096)],
    ['a differently cased value', 'CENTER'],
    ['an empty value', ''],
  ])('rejects %s as text alignment for every writer', (_name, value) => {
    const content = (attrs: Record<string, unknown>) => ({
      type: 'doc',
      content: [
        { type: 'heading', attrs: { level: 2, ...attrs }, content: [{ type: 'text', text: 'Title' }] },
        { type: 'paragraph', attrs, content: [{ type: 'text', text: 'Body' }] },
      ],
    })
    expect(() => editorJsonToYDoc(content({ textAlign: value }))).toThrow()

    const document = editorJsonToYDoc(content({ textAlign: 'center' }))
    const paragraph = document.getXmlFragment('content').get(1)
    if (!(paragraph instanceof Y.XmlElement)) throw new Error('fixture has no paragraph')
    paragraph.setAttribute('textAlign', value)
    expect(() => validateEditorYDoc(document)).toThrow()
    document.destroy()
  })

  it.each([null, 'left', 'center', 'right', 'justify'])(
    'accepts the offered text alignment %j',
    (value) => {
      const document = editorJsonToYDoc({
        type: 'doc',
        content: [{ type: 'paragraph', attrs: { textAlign: value }, content: [{ type: 'text', text: 'Body' }] }],
      })
      expect(validateEditorYDoc(document).content?.[0]?.attrs?.textAlign).toBe(value)
      document.destroy()
    },
  )

  // An attribute name is data. A plain object built from the attributes
  // cannot hold the key `__proto__`, so a comparison over one would never
  // see such an attribute, and it would travel hidden in the document.
  it.each([
    ['a block', 'element'],
    ['a text node', 'text'],
  ] as const)('rejects a hidden attribute named __proto__ on %s', (_name, target) => {
    const document = editorJsonToYDoc({
      type: 'doc',
      content: [{ type: 'paragraph', content: [{ type: 'text', text: 'Body' }] }],
    })
    const paragraph = document.getXmlFragment('content').get(0)
    if (!(paragraph instanceof Y.XmlElement)) throw new Error('fixture has no paragraph')
    const text = paragraph.get(0)
    if (!(text instanceof Y.XmlText)) throw new Error('fixture has no text')
    ;(target === 'element' ? paragraph : text).setAttribute('__proto__', 'hidden' as never)
    expect(() => validateEditorYDoc(document)).toThrow()
    document.destroy()
  })

  it('rejects keyed state materialized onto the editor XmlFragment root', () => {
    const source = new Y.Doc()
    source.getMap('content').set('rogue', 'hidden')
    const document = new Y.Doc()
    Y.applyUpdate(document, Y.encodeStateAsUpdate(source))

    expect(() => validateEditorYDoc(document)).toThrow(/non-canonical structure/)

    document.destroy()
    source.destroy()
  })

  it('retains codec-owned suggestion attributes while ignoring deleted Yjs history', () => {
    const schema = getSchema(createEditorSchemaExtensions({ enableUndoRedo: false }))
    const plain = schema.nodeFromJSON(parseEditorMarkdown('Hello'))
    const state = EditorState.create({ schema, doc: plain })
    const tracked = state.apply(transformToInqtrixSuggestionTransaction(
      state.tr.insertText('!', 6),
      state,
      {
        authorId: 'user-history',
        createdAt: 1_784_112_003,
        patchId: 'patch-history',
      },
      () => 'suggestion-history',
    )).doc
    const document = editorJsonToYDoc(tracked.toJSON())
    const text = firstParagraphXmlText(document)
    text.insert(text.length, '?')
    text.delete(text.length - 1, 1)
    text.setAttribute('transient', true)
    text.removeAttribute('transient')

    expect(validateEditorYDoc(document)).toEqual(tracked.toJSON())
    document.destroy()
  })

  it.each([
    ['struct dependency', 'struct'],
    ['delete dependency', 'delete'],
  ] as const)('rejects a document with an unresolved Yjs %s', (_label, kind) => {
    const document = editorJsonToYDoc(parseEditorMarkdown('Causal'))
    const replica = cloneYDoc(document)
    const text = firstParagraphXmlText(replica)
    text.insert(text.length, 'missing')
    const dependencyVector = Y.encodeStateVector(replica)
    if (kind === 'struct') text.insert(text.length, '!')
    else text.delete(text.length - 'missing'.length, 'missing'.length)
    const incomplete = Y.encodeStateAsUpdate(replica, dependencyVector)
    const candidate = cloneYDoc(document)
    Y.applyUpdate(candidate, incomplete)

    expect(editorYDocToJson(candidate)).toEqual(editorYDocToJson(document))
    expect(() => validateEditorYDoc(candidate)).toThrow(/unresolved/)

    candidate.destroy()
    replica.destroy()
    document.destroy()
  })

  it.each([
    ['element attribute', (document: Y.Doc) => {
      const paragraph = firstParagraphXml(document)
      paragraph.setAttribute('rogue', 'x'.repeat(64 * 1024))
      paragraph.removeAttribute('rogue')
    }],
    ['text attribute', (document: Y.Doc) => {
      const text = firstParagraphXmlText(document)
      text.setAttribute('rogue', 'x'.repeat(64 * 1024))
      text.removeAttribute('rogue')
    }],
    ['text content', (document: Y.Doc) => {
      const text = firstParagraphXmlText(document)
      text.insert(1, 'x'.repeat(64 * 1024))
      text.delete(1, 64 * 1024)
    }],
    ['text format', (document: Y.Doc) => {
      const text = firstParagraphXmlText(document)
      text.format(0, text.length, { rogue: 'x'.repeat(64 * 1024) })
      text.format(0, text.length, { rogue: null })
    }],
  ] as const)('rejects transient raw Yjs %s history', (_label, mutate) => {
    const document = editorJsonToYDoc(parseEditorMarkdown('History'))
    const replica = cloneYDoc(document)
    const vector = Y.encodeStateVector(replica)
    mutate(replica)
    const update = Y.encodeStateAsUpdate(replica, vector)

    expect(validateEditorYDoc(replica)).toEqual(editorYDocToJson(document))
    expect(() => validateSuggestionYjsUpdate(update)).toThrow(/Suggestion update/)

    replica.destroy()
    document.destroy()
  })

  it('rejects a raw update for an unknown shared root', () => {
    const document = editorJsonToYDoc(parseEditorMarkdown('Root'))
    const replica = cloneYDoc(document)
    const vector = Y.encodeStateVector(replica)
    const rogue = replica.getMap('rogue')
    rogue.set('payload', 'hidden')
    rogue.delete('payload')
    const update = Y.encodeStateAsUpdate(replica, vector)

    expect(() => validateSuggestionYjsUpdate(update)).toThrow(/parent/)

    replica.destroy()
    document.destroy()
  })

  it('rejects update-internal tombstones when the sender disables Yjs GC', () => {
    const document = editorJsonToYDoc(parseEditorMarkdown('No GC'))
    const replica = new Y.Doc({ gc: false })
    Y.applyUpdate(replica, Y.encodeStateAsUpdate(document))
    const vector = Y.encodeStateVector(replica)
    const text = firstParagraphXmlText(replica)
    text.insert(1, 'hidden')
    text.delete(1, 'hidden'.length)
    const update = Y.encodeStateAsUpdate(replica, vector)

    expect(() => validateSuggestionYjsUpdate(update)).toThrow(/transient/)

    replica.destroy()
    document.destroy()
  })

  it('allows resolved tombstones for existing content and repeat validation', () => {
    const document = editorJsonToYDoc(parseEditorMarkdown('Delete'))
    const replica = cloneYDoc(document)
    const vector = Y.encodeStateVector(replica)
    firstParagraphXmlText(replica).delete(0, 1)
    const update = Y.encodeStateAsUpdate(replica, vector)

    expect(validateSuggestionYjsUpdate(update)).toBe(update)
    expect(validateSuggestionYjsUpdate(update)).toBe(update)

    replica.destroy()
    document.destroy()
  })

  it('allows an origin-inferred structure attribute replacement update', () => {
    const document = editorJsonToYDoc(parseEditorMarkdown('Structure target'))
    const paragraph = firstParagraphXml(document)
    paragraph.setAttribute(INQTRIX_STRUCTURE_SUGGESTION_ATTR, null as never)
    const vector = Y.encodeStateVector(document)
    paragraph.setAttribute(INQTRIX_STRUCTURE_SUGGESTION_ATTR, {
      action: 'codeBlock',
      authorId: '0f111111-1111-4111-8111-111111111111',
      createdAt: 1_784_112_009,
      kind: 'structure',
      patchId: '0f222222-2222-4222-8222-222222222222',
      suggestionId: '0f333333-3333-4333-8333-333333333333',
    } as never)
    const update = Y.encodeStateAsUpdate(document, vector)
    const attributeReplacement = Y.decodeUpdate(update).structs.find((struct) => (
      struct instanceof Y.Item
      && struct.content instanceof Y.ContentAny
    ))

    expect(attributeReplacement).toMatchObject({ parentSub: null })
    expect(validateSuggestionYjsUpdate(update)).toBe(update)
    document.destroy()
  })

  it('tracks UUID suggestions with authoritative metadata and projects both views', () => {
    const schema = getSchema(createEditorSchemaExtensions({ enableUndoRedo: false }))
    const document = schema.node('doc', null, [schema.node('paragraph', null, schema.text('Hello'))])
    const state = EditorState.create({ schema, doc: document })
    const insertion = state.tr
      .setSelection(TextSelection.create(state.doc, 6))
      .insertText('!')
    const transformed = transformToInqtrixSuggestionTransaction(
      insertion,
      state,
      {
        authorId: 'user-1',
        createdAt: 1_784_112_000,
        patchId: '95bb8f85-5acd-4f50-a0cc-8af10a8e217b',
      },
      () => '7f8f4a45-867c-4551-92b5-593d7ff6801f',
    )
    const tracked = state.apply(transformed).doc

    expect(suggestionMarkKinds(tracked)).toEqual(['insertion'])
    expect(suggestionDescriptors(tracked)).toEqual([{
      authorId: 'user-1',
      createdAt: 1_784_112_000,
      kind: 'insertion',
      patchId: '95bb8f85-5acd-4f50-a0cc-8af10a8e217b',
      suggestionId: '7f8f4a45-867c-4551-92b5-593d7ff6801f',
    }])
    expect(projectFinalJson(tracked).content?.[0]?.content?.[0]?.text).toBe('Hello!')
    expect(projectOriginalJson(tracked).content?.[0]?.content?.[0]?.text).toBe('Hello')
  })

  it('extends an adjacent actor-owned insertion without requiring a second suggestion id', () => {
    const schema = getSchema(createEditorSchemaExtensions({ enableUndoRedo: false }))
    const document = schema.node('doc', null, [
      schema.node('paragraph', null, schema.text('Hello')),
    ])
    const initial = EditorState.create({ schema, doc: document })
    const first = initial.apply(transformToInqtrixSuggestionTransaction(
      initial.tr.insertText('A', 6),
      initial,
      {
        authorId: 'user-continuous',
        createdAt: 1_784_112_000,
        patchId: 'patch-continuous',
      },
      () => 'suggestion-existing',
    ))
    const continued = first.apply(transformToInqtrixSuggestionTransaction(
      first.tr.insertText('B', 7),
      first,
      {
        authorId: 'user-continuous',
        createdAt: 1_784_112_001,
        patchId: 'unused-new-patch',
      },
      () => 'unused-new-suggestion',
    )).doc

    expect(suggestionDescriptors(continued)).toEqual([{
      authorId: 'user-continuous',
      createdAt: 1_784_112_000,
      kind: 'insertion',
      patchId: 'patch-continuous',
      suggestionId: 'suggestion-existing',
    }])
    expect(projectFinalJson(continued).content?.[0]?.content?.[0]?.text).toBe('HelloAB')
    expect(projectOriginalJson(continued).content?.[0]?.content?.[0]?.text).toBe('Hello')
  })

  it('allows slash input inside an empty paragraph proposed by a split', () => {
    const schema = getSchema(createEditorSchemaExtensions({ enableUndoRedo: false }))
    const document = schema.node('doc', null, [
      schema.node('paragraph', null, schema.text('Before')),
    ])
    const initial = EditorState.create({ schema, doc: document })
    // Enter at the end of the paragraph: the only split suggest mode offers.
    const paragraphEnd = initial.doc.content.size - 1
    const split = initial.apply(transformToInqtrixSuggestionTransaction(
      initial.tr
        .setSelection(TextSelection.create(initial.doc, paragraphEnd))
        .split(paragraphEnd),
      initial,
      {
        authorId: 'user-slash',
        createdAt: 1_784_112_010,
        patchId: 'patch-split',
      },
      () => 'suggestion-split',
    ))
    expect(split.doc.childCount).toBe(2)
    const synchronized = EditorState.create({
      schema,
      doc: split.doc,
      selection: TextSelection.create(split.doc, split.doc.content.size - 1),
    })
    const slash = synchronized.apply(transformToInqtrixSuggestionTransaction(
      synchronized.tr.insertText('/'),
      synchronized,
      {
        authorId: 'user-slash',
        createdAt: 1_784_112_011,
        patchId: 'unused-slash-patch',
      },
      () => 'unused-slash-suggestion',
    )).doc

    expect(schema.nodeFromJSON(projectFinalJson(slash)).textContent).toContain('/')
    expect(projectOriginalJson(slash)).toEqual(document.toJSON())
  })

  it('models a text replacement as one replacement with both semantic projections', () => {
    const schema = getSchema(createEditorSchemaExtensions({ enableUndoRedo: false }))
    const document = schema.node('doc', null, [schema.node('paragraph', null, schema.text('Hello'))])
    const state = EditorState.create({ schema, doc: document })
    const replacement = state.tr.insertText('World', 1, 6)
    const transformed = transformToInqtrixSuggestionTransaction(
      replacement,
      state,
      {
        authorId: 'user-replacement',
        createdAt: 1_784_112_001,
        patchId: 'patch-replacement',
      },
      () => 'suggestion-replacement',
    )
    const tracked = state.apply(transformed).doc

    expect(suggestionMarkKinds(tracked)).toEqual(['deletion', 'insertion'])
    expect(suggestionDeclaredKinds(tracked)).toEqual(['replacement'])
    expect(suggestionDescriptors(tracked)).toEqual([{
      authorId: 'user-replacement',
      createdAt: 1_784_112_001,
      kind: 'replacement',
      patchId: 'patch-replacement',
      suggestionId: 'suggestion-replacement',
    }])
    expect(projectFinalJson(tracked).content?.[0]?.content?.[0]?.text).toBe('World')
    expect(projectOriginalJson(tracked).content?.[0]?.content?.[0]?.text).toBe('Hello')
    expect(serializeEditorJson(tracked.toJSON(), 'final').trim()).toBe('World')
    expect(serializeEditorJson(tracked.toJSON(), 'original').trim()).toBe('Hello')
    expect(editorYDocToJson(editorJsonToYDoc(tracked.toJSON()))).toEqual(tracked.toJSON())
  })

  it.each([
    ['distant ranges in one paragraph', false],
    ['ranges in separate paragraphs', true],
  ] as const)('rejects modification halves with %s', (_label, separateParents) => {
    const schema = getSchema(createEditorSchemaExtensions({ enableUndoRedo: false }))
    const deletion = schema.marks.deletion
    const insertion = schema.marks.insertion
    if (!deletion || !insertion) throw new Error('Editor schema is missing suggestion marks')
    const metadata = {
      authorId: 'user-distant',
      createdAt: 1_784_112_004,
      id: 'suggestion-distant',
      kind: 'modification',
      patchId: 'patch-distant',
      suggestionId: 'suggestion-distant',
    }
    const deleted = schema.text('Before', [deletion.create(metadata)])
    const inserted = schema.text('After', [insertion.create(metadata)])
    const document = separateParents
      ? schema.node('doc', null, [
          schema.node('paragraph', null, deleted),
          schema.node('paragraph', null, inserted),
        ])
      : schema.node('doc', null, [schema.node('paragraph', null, [
          deleted,
          schema.text(' unrelated '),
          inserted,
        ])])

    expect(() => suggestionDescriptors(document)).toThrow(/adjacent replacement ranges/)
  })

  it('requires both replacement halves to declare modification semantics', () => {
    const schema = getSchema(createEditorSchemaExtensions({ enableUndoRedo: false }))
    const deletion = schema.marks.deletion
    const insertion = schema.marks.insertion
    if (!deletion || !insertion) throw new Error('Editor schema is missing suggestion marks')
    const metadata = {
      authorId: 'user-semantic-pair',
      createdAt: 1_784_112_005,
      id: 'suggestion-semantic-pair',
      patchId: 'patch-semantic-pair',
      suggestionId: 'suggestion-semantic-pair',
    }
    const document = schema.node('doc', null, [schema.node('paragraph', null, [
      schema.text('Before', [deletion.create({ ...metadata, kind: 'deletion' })]),
      schema.text('After', [insertion.create({ ...metadata, kind: 'insertion' })]),
    ])])

    expect(() => suggestionDescriptors(document)).toThrow(/invalid mark composition/)
  })

  it('models inline formatting as one format proposal with both semantic projections', () => {
    const schema = getSchema(createEditorSchemaExtensions({ enableUndoRedo: false }))
    const bold = schema.marks.bold
    if (!bold) throw new Error('Editor schema is missing the bold mark')
    const document = schema.node('doc', null, [schema.node('paragraph', null, schema.text('Hello'))])
    const state = EditorState.create({ schema, doc: document })
    const formatting = state.tr.addMark(1, 6, bold.create())
    const transformed = transformToInqtrixSuggestionTransaction(
      formatting,
      state,
      {
        authorId: 'user-formatting',
        createdAt: 1_784_112_002,
        patchId: 'patch-formatting',
      },
      () => 'suggestion-formatting',
    )
    const tracked = state.apply(transformed).doc

    expect(suggestionMarkKinds(tracked)).toEqual(['deletion', 'insertion'])
    expect(suggestionDeclaredKinds(tracked)).toEqual(['format'])
    expect(suggestionDescriptors(tracked)).toEqual([{
      authorId: 'user-formatting',
      createdAt: 1_784_112_002,
      kind: 'format',
      patchId: 'patch-formatting',
      suggestionId: 'suggestion-formatting',
    }])
    expect(serializeEditorJson(tracked.toJSON(), 'final').trim()).toBe('**Hello**')
    expect(serializeEditorJson(tracked.toJSON(), 'original').trim()).toBe('Hello')
  })

  it('rejects conflicting or spoofed authoritative metadata', () => {
    const schema = getSchema(createEditorSchemaExtensions({ enableUndoRedo: false }))
    const deletion = schema.marks.deletion
    const insertion = schema.marks.insertion
    if (!deletion || !insertion) throw new Error('Editor schema is missing suggestion marks')
    const sharedAttributes = {
      createdAt: 1_784_112_003,
      id: 'suggestion-conflict',
      patchId: 'patch-conflict',
      suggestionId: 'suggestion-conflict',
    }
    const document = schema.node('doc', null, [
      schema.node('paragraph', null, [
        schema.text('Before', [deletion.create({
          ...sharedAttributes,
          authorId: 'user-one',
          kind: 'deletion',
        })]),
        schema.text('After', [insertion.create({
          ...sharedAttributes,
          authorId: 'user-two',
          kind: 'insertion',
        })]),
      ]),
    ])

    expect(() => suggestionDescriptors(document)).toThrow(/inconsistent metadata/)

    const spoofedKind = schema.node('doc', null, [
      schema.node('paragraph', null, [
        schema.text('After', [insertion.create({
          ...sharedAttributes,
          authorId: 'user-one',
          kind: 'deletion',
        })]),
      ]),
    ])
    expect(() => suggestionDescriptors(spoofedKind)).toThrow(/invalid mark composition/)

    const partialModification = schema.node('doc', null, [
      schema.node('paragraph', null, [
        schema.text('Before', [deletion.create({
          ...sharedAttributes,
          authorId: 'user-one',
          kind: 'modification',
        })]),
      ]),
    ])
    expect(() => suggestionDescriptors(partialModification)).toThrow(
      /invalid mark composition/,
    )
  })

  it('never transforms remote Yjs transactions into local suggestions', () => {
    const schema = getSchema(createEditorSchemaExtensions({ enableUndoRedo: false }))
    const document = schema.node('doc', null, [schema.node('paragraph', null, schema.text('A'))])
    const state = EditorState.create({ schema, doc: document })
    const remote = state.tr.insertText('B', 2).setMeta(ySyncPluginKey, { isChangeOrigin: true })

    const transformed = transformToInqtrixSuggestionTransaction(
      remote,
      state,
      { authorId: 'user-1', createdAt: 1, patchId: 'patch-1' },
      () => 'suggestion-1',
    )

    expect(transformed).toBe(remote)
    expect(state.apply(transformed).doc.textContent).toBe('AB')
  })

  it.each([
    ['heading2', 'heading'],
    ['blockquote', 'blockquote'],
    ['bulletList', 'bulletList'],
    ['orderedList', 'orderedList'],
    ['taskList', 'taskList'],
  ] as const)('tracks slash-menu %s as one reversible structure proposal', (action, finalType) => {
    const schema = getSchema(createEditorSchemaExtensions({ enableUndoRedo: false }))
    const document = schema.node('doc', null, [
      schema.node('paragraph', null, schema.text('/x')),
    ])
    const state = EditorState.create({ schema, doc: document })
    const direct = state.tr.delete(1, 3)
    if (action === 'heading2') {
      direct.setNodeMarkup(0, schema.nodes.heading, { level: 2, textAlign: null })
    } else {
      const target = direct.doc.nodeAt(0)
      const range = target
        ? direct.doc.resolve(0).blockRange(
            direct.doc.resolve(target.nodeSize),
          )
        : null
      const nodeType = schema.nodes[action]
      const wrapping = range && nodeType ? findWrapping(range, nodeType) : null
      if (!range || !wrapping) throw new Error(`Missing ${action} wrapping`)
      direct.wrap(range, wrapping)
    }
    direct.setMeta(INQTRIX_STRUCTURE_COMMAND_META, {
      action,
      commandRange: { from: 1, to: 3 },
    })
    const tracked = state.apply(transformToInqtrixSuggestionTransaction(
      direct,
      state,
      {
        authorId: 'user-structure',
        createdAt: 1_784_112_006,
        patchId: 'patch-structure',
      },
      () => 'suggestion-structure',
    )).doc

    expect(tracked.child(0).type.name).toBe('paragraph')
    expect(suggestionDescriptors(tracked)).toEqual([{
      authorId: 'user-structure',
      createdAt: 1_784_112_006,
      kind: 'structure',
      patchId: 'patch-structure',
      suggestionId: 'suggestion-structure',
    }])
    expect(projectFinalJson(tracked).content?.[0]?.type).toBe(finalType)
    expect(projectOriginalJson(tracked).content?.[0]?.content?.[0]?.text).toBe('/x')
    const accepted = resolveStructureSuggestion(
      EditorState.create({ schema, doc: tracked }),
      'suggestion-structure',
      'accept',
    )
    expect(accepted.doc.child(0).type.name).toBe(finalType)
    const rejected = resolveStructureSuggestion(
      EditorState.create({ schema, doc: tracked }),
      'suggestion-structure',
      'reject',
    )
    expect(rejected.doc.child(0).type.name).toBe('paragraph')
    expect(rejected.doc.textContent).toBe('')
  })

  it('supersedes a transported slash insertion with one structure proposal', () => {
    const schema = getSchema(createEditorSchemaExtensions({ enableUndoRedo: false }))
    const document = schema.node('doc', null, [
      schema.node('heading', { level: 1, textAlign: null }, schema.text('Title')),
    ])
    const initial = EditorState.create({ schema, doc: document })
    const trackedSlash = initial.apply(transformToInqtrixSuggestionTransaction(
      initial.tr.insertText('/', 1),
      initial,
      {
        authorId: 'user-structure',
        createdAt: 1_784_112_006,
        patchId: 'patch-slash',
      },
      () => 'suggestion-slash',
    )).doc
    const slashState = EditorState.create({ schema, doc: trackedSlash })
    const direct = slashState.tr
      .delete(1, 2)
      .setNodeMarkup(0, schema.nodes.heading, { level: 2, textAlign: null })
      .setMeta(INQTRIX_STRUCTURE_COMMAND_META, {
        action: 'heading2',
        commandRange: { from: 1, to: 2 },
      })
    const trackedStructure = slashState.apply(
      transformToInqtrixSuggestionTransaction(
        direct,
        slashState,
        {
          authorId: 'user-structure',
          createdAt: 1_784_112_007,
          patchId: 'unused-new-patch',
        },
        () => 'suggestion-structure',
      ),
    ).doc

    expect(suggestionDescriptors(trackedStructure)).toEqual([{
      authorId: 'user-structure',
      createdAt: 1_784_112_006,
      kind: 'structure',
      patchId: 'patch-slash',
      suggestionId: 'suggestion-structure',
    }])
    expect(projectOriginalJson(trackedStructure).content?.[0]).toEqual(
      expect.objectContaining({
        content: [{ text: 'Title', type: 'text' }],
        type: 'heading',
      }),
    )
    expect(projectFinalJson(trackedStructure).content?.[0]).toEqual(
      expect.objectContaining({
        attrs: expect.objectContaining({ level: 2 }),
        content: [{ text: 'Title', type: 'text' }],
        type: 'heading',
      }),
    )
  })

  it('rejects block insertions before the Yjs codec can drop their marks', () => {
    const schema = getSchema(createEditorSchemaExtensions({ enableUndoRedo: false }))
    const document = schema.node('doc', null, [schema.node('paragraph', null, schema.text('First'))])
    const state = EditorState.create({ schema, doc: document })
    const insertedParagraph = schema.node('paragraph', null, schema.text('Second'))
    const insertion = state.tr.insert(state.doc.content.size, insertedParagraph)
    expect(EDITOR_BLOCK_SUGGESTIONS_SUPPORTED).toBe(true)
    expect(() => transformToInqtrixSuggestionTransaction(
      insertion,
      state,
      { authorId: 'user-1', createdAt: 2, patchId: 'patch-block' },
      () => 'suggestion-block',
    )).toThrow(UnsupportedSuggestionStructureError)
  })

  it('rejects block deletions at the document boundary', () => {
    const schema = getSchema(createEditorSchemaExtensions({ enableUndoRedo: false }))
    const document = schema.node('doc', null, [
      schema.node('paragraph', null, schema.text('First')),
      schema.node('paragraph', null, schema.text('Second')),
    ])
    const state = EditorState.create({ schema, doc: document })
    const deletion = state.tr.delete(
      state.doc.child(0).nodeSize,
      state.doc.content.size,
    )
    expect(() => transformToInqtrixSuggestionTransaction(
      deletion,
      state,
      { authorId: 'user-1', createdAt: 3, patchId: 'patch-delete-block' },
      () => 'suggestion-delete-block',
    )).toThrow(UnsupportedSuggestionStructureError)
  })

  it('rejects an attribute-only node change in suggest mode', () => {
    // A code-block language switch is a pure AttrStep: the upstream
    // transform cannot represent it as a trackable suggestion, so the
    // guard must reject it hard — the UI therefore DISABLES the picker
    // in suggest mode instead of surfacing this error to the user.
    const schema = getSchema(createEditorSchemaExtensions({ enableUndoRedo: false }))
    const document = schema.node('doc', null, [
      schema.node('codeBlock', { language: 'python' }, schema.text('print(1)')),
    ])
    const state = EditorState.create({ schema, doc: document })
    const attrsOnly = state.tr.setNodeMarkup(0, undefined, {
      language: 'typescript',
    })
    expect(attrsOnly.docChanged).toBe(true)
    expect(() => transformToInqtrixSuggestionTransaction(
      attrsOnly,
      state,
      { authorId: 'user-1', createdAt: 5, patchId: 'patch-attrs-only' },
      () => 'suggestion-attrs-only',
    )).toThrow(UnsupportedSuggestionStructureError)
  })

  it('rejects block suggestion JSON instead of lossy Yjs round-tripping it', () => {
    const schema = getSchema(createEditorSchemaExtensions({ enableUndoRedo: false }))
    const insertion = schema.marks.insertion
    if (!insertion) throw new Error('Editor schema is missing insertion suggestions')
    const document = schema.nodeFromJSON({
      type: 'doc',
      content: [{
        type: 'paragraph',
        marks: [{
          type: 'insertion',
          attrs: {
            authorId: 'user-1',
            createdAt: 4,
            id: 'suggestion-block-codec',
            kind: 'insertion',
            patchId: 'patch-block-codec',
            suggestionId: 'suggestion-block-codec',
          },
        }],
        content: [{ type: 'text', text: 'Second' }],
      }],
    })

    expect(schema.nodes.doc?.spec.marks).not.toBe('insertion modification deletion')
    expect(() => editorJsonToYDoc(document.toJSON())).toThrow(
      UnsupportedSuggestionStructureError,
    )
  })

  it('rejects suggestion marks on the document root before Yjs conversion', () => {
    const schema = getSchema(createEditorSchemaExtensions({ enableUndoRedo: false }))
    const document = schema.nodeFromJSON({
      type: 'doc',
      marks: [{
        type: 'insertion',
        attrs: {
          authorId: 'user-root',
          createdAt: 5,
          id: 'suggestion-root',
          kind: 'insertion',
          patchId: 'patch-root',
          suggestionId: 'suggestion-root',
        },
      }],
      content: [{
        type: 'paragraph',
        content: [{ type: 'text', text: 'Root' }],
      }],
    })

    expect(() => editorJsonToYDoc(document.toJSON())).toThrow(
      UnsupportedSuggestionStructureError,
    )
  })

  it('rejects suggestion marks on inline math atoms before Yjs conversion', () => {
    const schema = getSchema(createEditorSchemaExtensions({ enableUndoRedo: false }))
    const insertion = schema.marks.insertion
    if (!insertion) throw new Error('Editor schema is missing insertion suggestions')
    const inlineMath = schema.node(
      'inlineMath',
      { latex: 'x^2' },
      undefined,
      [insertion.create({
        authorId: 'user-1',
        createdAt: 6,
        id: 'suggestion-inline-math',
        kind: 'insertion',
        patchId: 'patch-inline-math',
        suggestionId: 'suggestion-inline-math',
      })],
    )
    const document = schema.node('doc', null, [schema.node('paragraph', null, [inlineMath])])

    expect(() => editorJsonToYDoc(document.toJSON())).toThrow(
      UnsupportedSuggestionStructureError,
    )
  })

  it('serializes Yjs relative positions and keeps them stable across concurrent inserts', () => {
    const source = new Y.Doc()
    const text = source.getText('anchor')
    text.insert(0, 'abcd')
    const encoded = serializeRelativePosition(Y.createRelativePositionFromTypeIndex(text, 2))

    const replica = new Y.Doc()
    Y.applyUpdate(replica, Y.encodeStateAsUpdate(source))
    replica.getText('anchor').insert(0, 'XX')
    Y.applyUpdate(source, Y.encodeStateAsUpdate(replica, Y.encodeStateVector(source)))

    const absolute = Y.createAbsolutePositionFromRelativePosition(
      deserializeRelativePosition(encoded),
      source,
    )
    expect(absolute?.index).toBe(4)
  })

  it('adapts real y-tiptap positions across transformer-backed Yjs edits', () => {
    const schema = getSchema(createEditorSchemaExtensions({ enableUndoRedo: false }))
    const content = parseEditorMarkdown('abcd')
    const document = editorJsonToYDoc(content)
    const fragment = document.getXmlFragment('content')
    const initialized = initProseMirrorDoc(fragment, schema)
    const adapter = createRelativePositionAdapter(document, fragment, initialized.mapping)
    const encoded = adapter.fromProseMirrorPosition(3)

    expect(initialized.doc.toJSON()).toEqual(content)
    expect(adapter.toProseMirrorPosition(encoded)).toBe(3)

    const paragraph = fragment.get(0)
    const text = paragraph instanceof Y.XmlElement ? paragraph.get(0) : null
    if (!(text instanceof Y.XmlText)) throw new Error('Expected transformer-backed Yjs text')
    text.insert(0, 'XX')

    const remapped = initProseMirrorDoc(fragment, schema)
    const remappedAdapter = createRelativePositionAdapter(document, fragment, remapped.mapping)
    expect(remapped.doc.textContent).toBe('XXabcd')
    expect(remappedAdapter.toProseMirrorPosition(encoded)).toBe(5)

    expect(() => createRelativePositionAdapter(
      new Y.Doc(),
      fragment,
      remapped.mapping,
    )).toThrow(/must belong to the supplied Y.Doc/)
  })

  it('fails closed for unexpected or type-confused Yjs shared roots', () => {
    const content = parseEditorMarkdown('Canonical content')
    const unexpectedRoot = editorJsonToYDoc(content)
    unexpectedRoot.getMap('rogue').set('payload', 'not editor content')

    expect(() => validateEditorYDoc(unexpectedRoot)).toThrow(/only the content shared root/)

    const wrongContentType = new Y.Doc()
    wrongContentType.getMap('content').set('payload', 'not an XmlFragment')

    expect(() => validateEditorYDoc(wrongContentType)).toThrow(/must be an XmlFragment/)

    const encodedWrongType = new Y.Doc()
    Y.applyUpdate(encodedWrongType, Y.encodeStateAsUpdate(wrongContentType))
    expect(() => validateEditorYDoc(encodedWrongType)).toThrow(/non-canonical structure/)

    const textRoot = new Y.Doc()
    textRoot.getText('content').insert(0, 'not an XmlFragment')
    const encodedTextRoot = new Y.Doc()
    Y.applyUpdate(encodedTextRoot, Y.encodeStateAsUpdate(textRoot))
    expect(() => validateEditorYDoc(encodedTextRoot)).toThrow(/non-canonical structure/)

    const emptyContent = new Y.Doc()
    emptyContent.getXmlFragment('content')
    expect(() => validateEditorYDoc(emptyContent)).toThrow()
  })

  it('produces a deterministic schema fingerprint and strict room names', async () => {
    await expect(getEditorSchemaFingerprint()).resolves.toMatch(/^[a-f0-9]{64}$/)
    await expect(getEditorSchemaFingerprint()).resolves.toBe(await getEditorSchemaFingerprint())
    expect(editorCollaborationRoom('ed_123', 4)).toBe('inqtrix-editor-v1:ed_123:g4')
    expect(parseEditorCollaborationRoom('inqtrix-editor-v1:ed_123:g4')).toEqual({
      documentId: 'ed_123',
      generation: 4,
    })
    expect(parseEditorCollaborationRoom('inqtrix-editor-v1:ed_123:g0')).toBeNull()
    expect(editorCollaborationRoom('editor-doc-123', 1)).toBe(
      'inqtrix-editor-v1:editor-doc-123:g1',
    )
  })

  it('changes the fingerprint for schema order, rank, shape, and behavior inputs', async () => {
    const descriptor = editorSchemaDescriptor()
    const baseline = await fingerprintEditorSchemaDescriptor(descriptor)
    await expect(getEditorSchemaFingerprint()).resolves.toBe(baseline)

    const reorderedNodes = structuredClone(descriptor)
    const firstNode = reorderedNodes.nodes[0]
    const secondNode = reorderedNodes.nodes[1]
    if (!firstNode || !secondNode) throw new Error('Schema must contain at least two nodes')
    reorderedNodes.nodes.splice(0, 2, secondNode, firstNode)
    reorderedNodes.nodes.forEach((node, order) => { node.order = order })

    const reorderedMarks = structuredClone(descriptor)
    const firstMark = reorderedMarks.marks[0]
    const secondMark = reorderedMarks.marks[1]
    if (!firstMark || !secondMark) throw new Error('Schema must contain at least two marks')
    reorderedMarks.marks.splice(0, 2, secondMark, firstMark)
    reorderedMarks.marks.forEach((mark, order) => {
      mark.order = order
      mark.rank = order
    })

    const changedRank = structuredClone(descriptor)
    const rankedMark = changedRank.marks[0]
    if (!rankedMark || rankedMark.rank === null) throw new Error('Schema must contain ranked marks')
    rankedMark.rank += 1

    const changedShape = structuredClone(descriptor)
    const paragraph = changedShape.nodes.find((node) => node.name === 'paragraph')
    if (!paragraph) throw new Error('Schema must contain a paragraph node')
    paragraph.spec.content = 'text*'

    const changedBehavior = {
      ...descriptor,
      behavior: {
        ...descriptor.behavior,
        suggestionTransform: 'changed-suggestion-behavior',
      },
    }

    const changedVersion = {
      ...descriptor,
      dependencies: {
        ...descriptor.dependencies,
        tiptap: '3.27.2',
      },
    }

    const mutations = await Promise.all([
      fingerprintEditorSchemaDescriptor(reorderedNodes),
      fingerprintEditorSchemaDescriptor(reorderedMarks),
      fingerprintEditorSchemaDescriptor(changedRank),
      fingerprintEditorSchemaDescriptor(changedShape),
      fingerprintEditorSchemaDescriptor(changedBehavior),
      fingerprintEditorSchemaDescriptor(changedVersion),
    ])
    expect(new Set(mutations).size).toBe(mutations.length)
    for (const mutation of mutations) expect(mutation).not.toBe(baseline)
  })
})
