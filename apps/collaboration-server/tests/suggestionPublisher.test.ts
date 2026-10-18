import { getSchema } from '@tiptap/core'
import { EditorState } from '@tiptap/pm/state'
import { initProseMirrorDoc, updateYFragment } from '@tiptap/y-tiptap'
import {
  EDITOR_YJS_FRAGMENT,
  createEditorSchemaExtensions,
  editorJsonToYDoc,
  editorYDocToJson,
  parseEditorMarkdown,
  serializeEditorJson,
  transformToInqtrixSuggestionTransaction,
} from '@inqtrix/editor-schema'

import * as Y from 'yjs'

import { resolvePatchDecision } from '../src/decisionResolver'
import { validateDocument } from '../src/documentState'
import { CollaborationError } from '../src/errors'
import { validateSuggestionUpdate } from '../src/suggestPolicy'
import {
  MAX_PROPOSED_BLOCK_BREAKS,
  parsePublicationBase,
  parseRoundTrippableTarget,
  publishTargetSuggestion,
} from '../src/suggestionPublisher'
import { USER_ID } from './helpers'

const PATCH_ID = '22222222-2222-4222-8222-222222222222'
const SUGGESTION_ID = '33333333-3333-4333-8333-333333333333'
const EXISTING_PATCH_ID = '44444444-4444-4444-8444-444444444444'
const EXISTING_SUGGESTION_ID = '55555555-5555-4555-8555-555555555555'
const OTHER_USER_ID = '66666666-6666-4666-8666-666666666666'
const CREATED_AT = 1_784_112_000
const schema = getSchema(createEditorSchemaExtensions({ enableUndoRedo: false }))

describe('target Markdown suggestion publication', () => {
  it('creates a server-attributed suggestion without replacing the Y.Doc', () => {
    const document = editorJsonToYDoc(parseEditorMarkdown('Hello'))

    const result = publishTargetSuggestion(document, {
      actorUserId: USER_ID,
      patchId: PATCH_ID,
      ...markdownTarget('Hello world'),
    }, {
      createSuggestionId: () => SUGGESTION_ID,
      nowMilliseconds: () => CREATED_AT,
    })

    const json = editorYDocToJson(document)
    expect(serializeEditorJson(json, 'original').trim()).toBe('Hello')
    expect(serializeEditorJson(json, 'final').trim()).toBe('Hello world')
    expect(result).toEqual({
      patchIds: [PATCH_ID],
      placement: null,
      suggestionIds: [SUGGESTION_ID],
      suggestions: [{
        authorId: USER_ID,
        createdAt: CREATED_AT,
        kind: 'insertion',
        patchId: PATCH_ID,
        suggestionId: SUGGESTION_ID,
      }],
    })
  })

  it('preserves an existing open suggestion outside the target diff', () => {
    const plain = schema.nodeFromJSON(parseEditorMarkdown('Hello\n\nSecond'))
    const state = EditorState.create({ schema, doc: plain })
    const tracked = transformToInqtrixSuggestionTransaction(
      state.tr.insertText('!', 6),
      state,
      {
        authorId: USER_ID,
        createdAt: CREATED_AT - 10,
        patchId: EXISTING_PATCH_ID,
      },
      () => EXISTING_SUGGESTION_ID,
    ).doc
    const document = editorJsonToYDoc(tracked.toJSON())

    publishTargetSuggestion(document, {
      actorUserId: USER_ID,
      patchId: PATCH_ID,
      ...markdownTarget('Hello!\n\nChanged'),
    }, {
      createSuggestionId: () => SUGGESTION_ID,
      nowMilliseconds: () => CREATED_AT,
    })

    const json = editorYDocToJson(document)
    expect(serializeEditorJson(json, 'original').trim()).toBe('Hello\n\nSecond')
    expect(serializeEditorJson(json, 'final').trim()).toBe('Hello!\n\nChanged')
    expect(JSON.stringify(json)).toContain(EXISTING_SUGGESTION_ID)
    expect(JSON.stringify(json)).toContain(SUGGESTION_ID)
  })

  it('publishes a direct edit before an unchanged foreign suggestion', () => {
    const plain = schema.nodeFromJSON(parseEditorMarkdown('First\n\nSecond'))
    const state = EditorState.create({ schema, doc: plain })
    const tracked = transformToInqtrixSuggestionTransaction(
      state.tr.insertText('!', state.doc.content.size - 1),
      state,
      {
        authorId: OTHER_USER_ID,
        createdAt: CREATED_AT - 10,
        patchId: EXISTING_PATCH_ID,
      },
      () => EXISTING_SUGGESTION_ID,
    ).doc
    const document = editorJsonToYDoc(tracked.toJSON())

    publishTargetSuggestion(document, {
      actorUserId: USER_ID,
      patchId: PATCH_ID,
      ...markdownTarget('Changed first\n\nSecond!'),
    }, {
      createSuggestionId: () => SUGGESTION_ID,
      nowMilliseconds: () => CREATED_AT,
    })

    const json = editorYDocToJson(document)
    expect(serializeEditorJson(json, 'final').trim()).toBe('Changed first\n\nSecond!')
    expect(JSON.stringify(json)).toContain(EXISTING_SUGGESTION_ID)
    expect(JSON.stringify(json)).toContain(SUGGESTION_ID)
  })

  it('publishes before an unchanged foreign suggestion in the same paragraph', () => {
    const plain = schema.nodeFromJSON(parseEditorMarkdown('Hello'))
    const state = EditorState.create({ schema, doc: plain })
    const tracked = transformToInqtrixSuggestionTransaction(
      state.tr.insertText('!', state.doc.content.size - 1),
      state,
      {
        authorId: OTHER_USER_ID,
        createdAt: CREATED_AT - 10,
        patchId: EXISTING_PATCH_ID,
      },
      () => EXISTING_SUGGESTION_ID,
    ).doc
    const document = editorJsonToYDoc(tracked.toJSON())

    publishTargetSuggestion(document, {
      actorUserId: USER_ID,
      patchId: PATCH_ID,
      ...markdownTarget('Prefix Hello!'),
    }, {
      createSuggestionId: () => SUGGESTION_ID,
      nowMilliseconds: () => CREATED_AT,
    })

    const json = editorYDocToJson(document)
    expect(serializeEditorJson(json, 'final').trim()).toBe('Prefix Hello!')
    expect(JSON.stringify(json)).toContain(EXISTING_SUGGESTION_ID)
    expect(JSON.stringify(json)).toContain(SUGGESTION_ID)
  })

  it('places a target built on an older projection beside a pending suggestion and a later edit', () => {
    const plain = schema.nodeFromJSON(parseEditorMarkdown('Hello\n\nSecond\n\nThird'))
    const state = EditorState.create({ schema, doc: plain })
    const tracked = transformToInqtrixSuggestionTransaction(
      state.tr.insertText('!', 14),
      state,
      {
        authorId: OTHER_USER_ID,
        createdAt: CREATED_AT - 10,
        patchId: EXISTING_PATCH_ID,
      },
      () => EXISTING_SUGGESTION_ID,
    ).doc
    const document = editorJsonToYDoc(tracked.toJSON())
    const base = serializeEditorJson(editorYDocToJson(document), 'final')
    expect(base.trim()).toBe('Hello\n\nSecond!\n\nThird')
    // Another writer types after the projection was read.
    const fragment = document.getXmlFragment(EDITOR_YJS_FRAGMENT)
    const initialized = initProseMirrorDoc(fragment, schema)
    const typed = EditorState.create({ doc: initialized.doc, schema })
    const end = typed.doc.content.size - 1
    document.transact(() => updateYFragment(
      document,
      fragment,
      typed.tr.insertText(' MARKE1', end).doc,
      initialized.meta,
    ))

    const result = publishTargetSuggestion(document, {
      actorUserId: USER_ID,
      base: parsePublicationBase(base),
      patchId: PATCH_ID,
      target: parseRoundTrippableTarget(base.replace('Hello', 'Hello world')),
    }, {
      createSuggestionId: () => SUGGESTION_ID,
      nowMilliseconds: () => CREATED_AT,
    })

    expect(result.placement).toBe('rebased')
    const json = editorYDocToJson(document)
    expect(serializeEditorJson(json, 'original').trim()).toBe('Hello\n\nSecond\n\nThird MARKE1')
    expect(serializeEditorJson(json, 'final').trim()).toBe('Hello world\n\nSecond!\n\nThird MARKE1')
    expect(JSON.stringify(json)).toContain(EXISTING_SUGGESTION_ID)
    expect(JSON.stringify(json)).toContain(SUGGESTION_ID)
  })

  it('rejects a no-op target rather than creating an empty patch', () => {
    const document = editorJsonToYDoc(parseEditorMarkdown('Hello'))

    expect(() => publishTargetSuggestion(document, {
      actorUserId: USER_ID,
      patchId: PATCH_ID,
      ...markdownTarget('Hello'),
    })).toThrowError('suggestion_conflict')
  })

  it('rejects a target that would replace a math atom', () => {
    const document = editorJsonToYDoc(parseEditorMarkdown('Equation: $x + 1$.'))

    expect(() => publishTargetSuggestion(document, {
      actorUserId: USER_ID,
      patchId: PATCH_ID,
      ...markdownTarget('Equation: $x + 2$.'),
    })).toThrowError('unsupported_suggestion_structure')
  })

  it('publishes a paragraph inserted between two paragraphs as a break at the end of the first', () => {
    const source = 'First paragraph.\n\nSecond paragraph.'
    const target = 'First paragraph.\n\nNew between.\n\nSecond paragraph.'
    const document = editorJsonToYDoc(parseEditorMarkdown(source))
    let next = 0

    const result = publishTargetSuggestion(document, {
      actorUserId: USER_ID,
      patchId: PATCH_ID,
      ...markdownTarget(target),
    }, {
      createSuggestionId: () => `33333333-3333-4333-8333-${String(++next).padStart(12, '0')}`,
      nowMilliseconds: () => CREATED_AT,
    })

    const json = editorYDocToJson(document)
    expect(serializeEditorJson(json, 'original').trim()).toBe(source)
    expect(serializeEditorJson(json, 'final').trim()).toBe(target)
    // No original text moved into the new block: the second paragraph keeps
    // its text unmarked.
    const second = schema.nodeFromJSON(json).child(2)
    expect(second.textContent).toBe('Second paragraph.')
    expect(second.firstChild?.marks).toEqual([])

    const accepted = editorJsonToYDoc(json)
    resolvePatchDecision(accepted, { decision: 'accept', patchIds: result.patchIds })
    const acceptedJson = editorYDocToJson(accepted)
    expect(serializeEditorJson(acceptedJson, 'final').trim()).toBe(target)
    expect(JSON.stringify(acceptedJson)).not.toContain('\u200B')

    const rejected = editorJsonToYDoc(json)
    resolvePatchDecision(rejected, { decision: 'reject', patchIds: result.patchIds })
    expect(editorYDocToJson(rejected)).toEqual(parseEditorMarkdown(source))
  })

  it('publishes beside a pending proposed paragraph without touching its anchors', () => {
    const plain = schema.nodeFromJSON(parseEditorMarkdown('First paragraph.\n\nSecond paragraph.'))
    const initial = EditorState.create({ schema, doc: plain })
    let next = 0
    const otherIds = () => `55555555-5555-4555-8555-${String(++next).padStart(12, '0')}`
    const metadata = { authorId: OTHER_USER_ID, createdAt: CREATED_AT - 10, patchId: EXISTING_PATCH_ID }
    const firstEnd = plain.child(0).nodeSize - 1
    const split = initial.apply(transformToInqtrixSuggestionTransaction(
      initial.tr.split(firstEnd),
      initial,
      metadata,
      otherIds,
    ))
    const proposed = split.apply(transformToInqtrixSuggestionTransaction(
      split.tr.insertText('Proposed', firstEnd + 3),
      split,
      metadata,
      otherIds,
    )).doc
    const document = editorJsonToYDoc(proposed.toJSON())
    const pendingBefore = JSON.stringify(proposed.child(1).toJSON())

    publishTargetSuggestion(document, {
      actorUserId: USER_ID,
      patchId: PATCH_ID,
      ...markdownTarget('First paragraph.\n\nProposed\n\nSecond paragraph changed.'),
    }, {
      createSuggestionId: () => SUGGESTION_ID,
      nowMilliseconds: () => CREATED_AT,
    })

    const json = editorYDocToJson(document)
    expect(serializeEditorJson(json, 'final').trim())
      .toBe('First paragraph.\n\nProposed\n\nSecond paragraph changed.')
    expect(serializeEditorJson(json, 'original').trim())
      .toBe('First paragraph.\n\nSecond paragraph.')
    expect(JSON.stringify(schema.nodeFromJSON(json).child(1).toJSON())).toBe(pendingBefore)
  })

  it('publishes a paragraph appended at the end of the document', () => {
    const outcome = publishShape('First', 'First\n\nSecond')

    expectPublishedShape(outcome)
  })

  it('publishes a paragraph inserted before a heading as a break at the end of the paragraph before it', () => {
    const source = 'Bei Fragen hilft der Support weiter.\n\n## Schreibzone\n\nText.'
    const target = 'Bei Fragen hilft der Support weiter.\n\nNeuer Satz am Ende des Abschnitts.\n\n## Schreibzone\n\nText.'

    const outcome = publishShape(source, target)

    expectPublishedShape(outcome)
    // No existing text moved: the heading keeps its text unmarked.
    const heading = schema.nodeFromJSON(outcome.json).child(2)
    expect(heading.type.name).toBe('heading')
    expect(heading.textContent).toBe('Schreibzone')
    expect(heading.firstChild?.marks).toEqual([])
  })

  it('publishes several new paragraphs as one break per paragraph', () => {
    const outcome = publishShape('Intro.\n\n## Next', 'Intro.\n\nOne.\n\nTwo.\n\nThree.\n\n## Next')

    expectPublishedShape(outcome)
    const document = schema.nodeFromJSON(outcome.json)
    for (const index of [1, 2, 3]) {
      // Every new block holds only the actor's inserted text.
      document.child(index).forEach((child) => {
        expect(child.marks.map((mark) => mark.type.name)).toContain('insertion')
      })
    }
  })

  it('publishes several new paragraphs between two paragraphs as one break per paragraph', () => {
    // Where a paragraph follows, the difference starts inside it. One new
    // paragraph was already proposed at the end of the paragraph before it;
    // two or more were refused, the inner ones arriving as whole blocks.
    const outcome = publishShape('Intro.\n\nOutro.', 'Intro.\n\nFirst.\n\nSecond.\n\nThird.\n\nOutro.')

    expectPublishedShape(outcome)
    const document = schema.nodeFromJSON(outcome.json)
    for (const index of [1, 2, 3]) {
      // Every new block holds only the actor's inserted text.
      document.child(index).forEach((child) => {
        expect(child.marks.map((mark) => mark.type.name)).toContain('insertion')
      })
    }
    // No existing text moved: the following paragraph keeps its text unmarked.
    expect(document.child(4).textContent).toBe('Outro.')
    expect(document.child(4).firstChild?.marks).toEqual([])
  })

  it('publishes new paragraphs that begin like the paragraph after them', () => {
    // The difference then starts inside the following paragraph, after the
    // shared beginning ("Die"). Inserted there, the new block would take
    // that paragraph's text with it; at its start it is the same result.
    const source = 'Die Frist endet morgen.\n\nDie Kosten steigen.'
    for (const target of [
      'Die Frist endet morgen.\n\nDie Abgabe erfolgt online.\n\nDie Kosten steigen.',
      'Die Frist endet morgen.\n\nDie Abgabe erfolgt online.\n\nDie Kosten sinken nicht.\n\nDie Kosten steigen.',
    ]) {
      const outcome = publishShape(source, target)

      expectPublishedShape(outcome)
      const document = schema.nodeFromJSON(outcome.json)
      const following = document.child(document.childCount - 1)
      expect(following.textContent, target).toBe('Die Kosten steigen.')
      expect(following.firstChild?.marks, target).toEqual([])
    }
  })

  it('leaves an insertion within a paragraph where the difference found it', () => {
    // Only new blocks move to the start of the paragraph they precede; text
    // typed into a paragraph that repeats its own beginning stays in place.
    const outcome = publishShape('Die Die Frist.', 'Die Die Die Frist.')

    expectPublishedShape(outcome)
    const paragraph = schema.nodeFromJSON(outcome.json).child(0)
    expect(paragraph.child(0).text).toBe('Die Die ')
    expect(paragraph.child(0).marks).toEqual([])
  })

  it('publishes a target whose paragraph begins like a Markdown block', () => {
    // The canonical serialization escapes the marker, so the target reads
    // back as the same paragraph instead of a heading.
    expectPublishedShape(publishShape('Davor.\n\nDer Bereich teilt', 'Davor.\n\n\\## Der Bereich teilt'))
  })

  it('refuses a break inside a paragraph that would move its text, by that rule', () => {
    const refused = publishShape('Hello world.', 'Hello\n\nNew world.')

    expect(refused.error?.suggestionReason).toBe('split_moves_content')
    expect(refused.unchanged).toBe(true)
  })

  it('publishes a new list item after the last item', () => {
    expectPublishedShape(publishShape('- a\n- b\n\n## H', '- a\n- b\n- c\n\n## H'))
  })

  it('every published shape passes the suggest-mode update policy and every refused one leaves the document unchanged', () => {
    const published: Array<[string, string]> = [
      ['P one.\n\n## H', 'P one.\n\nNew.\n\n## H'],
      ['P one.', 'P one.\n\nNew.'],
      ['P one.\n\n| a | b |\n| - | - |\n| 1 | 2 |', 'P one.\n\nNew.\n\n| a | b |\n| - | - |\n| 1 | 2 |'],
      ['## A\n\n## B', '## A\n\nNew.\n\n## B'],
      ['P.\n\n## Last', 'P.\n\n## Last\n\nNew.'],
      ['- a\n\n## H', '- a\n\n  New in item.\n\n## H'],
      ['- a\n\n## H', '- a\n- b\n- c\n\n## H'],
      ['- [ ] a\n\n## H', '- [ ] a\n- [ ] b\n\n## H'],
      ['P.\n\n## H', 'P.\n\nOne.\n\nTwo.\n\n## H'],
      ['P.\n\n## H', 'P.\n\nOne **bold** and *em*.\n\n## H'],
      ['> Q.\n\n## H', '> Q.\n>\n> New.\n\n## H'],
      ['- a\n  - b\n- c', '- a\n  - b\n  - b2\n- c'],
      ['P one.\n\n- a', 'P one.\n\nNew.\n\n- a'],
      ['P.\n\nQ.', 'P.\n\nOne.\n\nTwo.\n\nQ.'],
      ['Intro.\n\nOutro.', 'Intro.\n\nOne.\n\nOutro.'],
      // The new paragraph begins with all of the one after it: the difference
      // ends that paragraph, and it is extended and followed by a new one.
      ['Die', 'Die Frist.\n\nDie'],
      ['## H\n\nP.', '## H\n\nOne.\n\nTwo.\n\nP.'],
      ['> Q.\n>\n> R.', '> Q.\n>\n> One.\n>\n> Two.\n>\n> R.'],
      // The one shape after a code block: a single new paragraph where a
      // paragraph follows, proposed at the end of the code block.
      ['```\ncode\n```\n\nP.', '```\ncode\n```\n\nNew.\n\nP.'],
    ]
    for (const [source, target] of published) {
      expectPublishedShape(publishShape(source, target), `${source} -> ${target}`)
    }

    const refused: Array<[string, string]> = [
      ['P.', 'P.\n\n| a | b |\n| - | - |\n| 1 | 2 |'],
      ['P one.\n\n## H', 'P one.\n\n> Quote.\n\n## H'],
      ['> Q.\n\n## H', '> Q.\n\nNew.\n\n## H'],
      ['- a\n\n## H', '- a\n- b\n  - c\n\n## H'],
      ['| a | b |\n| - | - |\n| 1 | 2 |\n\n## H', '| a | b |\n| - | - |\n| 1 | 2 |\n\nNew.\n\n## H'],
      ['```\ncode\n```\n\n## H', '```\ncode\n```\n\nNew.\n\n## H'],
      ['P.\n\n---\n\n## H', 'P.\n\n---\n\nNew.\n\n## H'],
      ['P.\n\n## H', 'P.\n\nNew.\n\n## New H\n\n## H'],
      ['P.\n\n## H', 'P.\n\n​New.\n\n## H'],
      ['- a\n- b', '- a\n- x\n- b'],
      ['- [ ] a\n\n## H', '- [ ] a\n- [x] b\n\n## H'],
      ['P.\n\n## H', 'P.\n\nLine one  \nLine two.\n\n## H'],
      ['- a\n\n## H', '- a\n\nAfter the list.\n\n## H'],
      ['```\ncode\n```\n\nP.', '```\ncode\n```\n\nOne.\n\nTwo.\n\nP.'],
      ['- a\n\nP.', '- a\n\nOne.\n\nTwo.\n\nP.'],
    ]
    for (const [source, target] of refused) {
      const outcome = publishShape(source, target)
      expect(outcome.error, `${source} -> ${target}`).toBeInstanceOf(CollaborationError)
      expect(outcome.unchanged, `${source} -> ${target}`).toBe(true)
    }
  })

  it('the suggest-mode policy refuses every hostile change to a published block suggestion, by its own reason', () => {
    // A suggest-only writer's update, made the way its editor binding makes
    // it: a minimal change to its replica of the published document.
    const published = publishShape('P.\n\n## H', 'P.\n\nNew one.\n\nNew two.\n\n## H')
    expectPublishedShape(published)
    const hostile = (edit: (json: JSONContentLike) => void): unknown => {
      const server = new Y.Doc()
      Y.applyUpdate(server, Y.encodeStateAsUpdate(published.document))
      const beforeJson = editorYDocToJson(server)
      const beforeDocument = new Y.Doc()
      Y.applyUpdate(beforeDocument, Y.encodeStateAsUpdate(server))
      const vector = Y.encodeStateVector(server)
      const replica = new Y.Doc()
      Y.applyUpdate(replica, Y.encodeStateAsUpdate(server))
      const fragment = replica.getXmlFragment(EDITOR_YJS_FRAGMENT)
      const initialized = initProseMirrorDoc(fragment, schema)
      const next = initialized.doc.toJSON() as JSONContentLike
      edit(next)
      updateYFragment(replica, fragment, schema.nodeFromJSON(next), initialized.meta)
      const update = Y.encodeStateAsUpdate(replica, vector)
      Y.applyUpdate(server, update)
      try {
        validateSuggestionUpdate(beforeJson, editorYDocToJson(server), 'suggest', OTHER_USER_ID, {
          afterDocument: server,
          beforeDocument,
          deletedByUpdate: Y.decodeUpdate(update).ds,
        })
      } catch (caught) {
        return caught
      }
      return null
    }
    const blockWith = (json: JSONContentLike, text: string) => (json.content ?? [])
      .findIndex((block) => JSON.stringify(block).includes(text))
    const texts = (node: JSONContentLike, visit: (text: JSONContentLike) => void): void => {
      if (node.type === 'text') visit(node)
      for (const child of node.content ?? []) texts(child, visit)
    }

    // Control: an update that changes nothing passes, so a refusal below is
    // the policy's answer to the change, not to the harness.
    expect(hostile(() => undefined)).toBeNull()

    const changes: Record<string, (json: JSONContentLike) => void> = {
      'an unmarked paragraph after the new blocks': (json) => {
        json.content!.splice(blockWith(json, 'New two.') + 1, 0, {
          content: [{ text: 'Smuggled.', type: 'text' }],
          type: 'paragraph',
        })
      },
      'unmarked text inside a new block': (json) => {
        json.content![blockWith(json, 'New two.')]!.content!.push({ text: ' Smuggled.', type: 'text' })
      },
      'the insertion marks stripped from the new blocks': (json) => texts(json, (text) => {
        if (text.text === 'New one.' || text.text === 'New two.') delete text.marks
      }),
      'the text of another writer\'s new block rewritten': (json) => texts(json, (text) => {
        if (text.text === 'New one.') text.text = 'Rewritten.'
      }),
      'another writer\'s new block removed': (json) => {
        json.content!.splice(blockWith(json, 'New two.'), 1)
      },
    }
    for (const [label, change] of Object.entries(changes)) {
      const refusal = hostile(change)
      expect(refusal, label).toBeInstanceOf(CollaborationError)
      expect((refusal as CollaborationError).reason, label).toBe('suggestion_policy_violation')
    }
  })

  it('still refuses a new heading and a code block, naming the reason', () => {
    for (const target of [
      'P.\n\n## New heading\n\n## H',
      'P.\n\n```\nnew code\n```\n\n## H',
    ]) {
      const outcome = publishShape('P.\n\n## H', target)
      expect(outcome.error?.reason, target).toBe('unsupported_suggestion_structure')
      expect(outcome.error?.suggestionReason, target).toBe('proposed_block_unsupported')
      expect(outcome.unchanged, target).toBe(true)
    }
  })

  it('refuses a paragraph after a code block, whose end no break can carry', () => {
    const outcome = publishShape('```\ncode\n```\n\n## H', '```\ncode\n```\n\nNew.\n\n## H')

    expect(outcome.error?.reason).toBe('unsupported_suggestion_structure')
    expect(outcome.error?.suggestionReason).toBe('node_marks_unsupported')
    expect(outcome.unchanged).toBe(true)
  })

  it('still refuses a new list item between two items', () => {
    const outcome = publishShape('- a\n- b', '- a\n- x\n- b')

    expect(outcome.error?.suggestionReason).toBe('split_moves_content')
    expect(outcome.unchanged).toBe(true)
  })

  it('refuses next to another writer\'s pending insertion at the block end, document unchanged', () => {
    const plain = schema.nodeFromJSON(parseEditorMarkdown('P one.\n\n## H'))
    const state = EditorState.create({ schema, doc: plain })
    const tracked = transformToInqtrixSuggestionTransaction(
      state.tr.insertText(' theirs', plain.child(0).nodeSize - 1),
      state,
      { authorId: OTHER_USER_ID, createdAt: CREATED_AT - 10, patchId: EXISTING_PATCH_ID },
      () => EXISTING_SUGGESTION_ID,
    ).doc

    const outcome = publishShape(tracked.toJSON(), 'P one. theirs\n\nNew.\n\n## H')

    expect(outcome.error?.reason).toBe('unsupported_suggestion_structure')
    expect(outcome.error?.suggestionReason).toBe('foreign_suggestion_changed')
    expect(outcome.unchanged).toBe(true)
  })

  it('names the guard that refused when the library gives no reason', () => {
    const duplicate = editorJsonToYDoc(parseEditorMarkdown('Hello'))
    publishTargetSuggestion(duplicate, {
      actorUserId: USER_ID,
      patchId: PATCH_ID,
      ...markdownTarget('Hello world'),
    }, { createSuggestionId: () => SUGGESTION_ID, nowMilliseconds: () => CREATED_AT })

    expect(refusalOf(() => publishTargetSuggestion(duplicate, {
      actorUserId: USER_ID,
      patchId: PATCH_ID,
      ...markdownTarget('Hello world again'),
    }))?.suggestionReason).toBe('patch_already_published')
    expect(publishShape('Hello', 'Hello').error?.suggestionReason).toBe('target_unchanged')
    expect(publishShape('P.\n\n## H', 'P.\n\nLine one  \nLine two.\n\n## H').error?.suggestionReason)
      .toBe('non_text_atoms_changed')
  })

  it('refuses more new blocks than one publication may propose, before any suggestion work on them', () => {
    const paragraphs = (count: number) => Array.from({ length: count }, (_, index) => `New ${index}.`)
    const source = 'P one.\n\n## H'

    // Parsing a target of this size costs the same for every refusal; a
    // hard break makes the publisher refuse it before any diff work.
    const baselineStartedAt = performance.now()
    const baseline = publishShape(source, ['P one.', ...paragraphs(2_000), 'a  \nb', '## H'].join('\n\n'))
    const baselineElapsed = performance.now() - baselineStartedAt
    expect(baseline.error?.suggestionReason).toBe('non_text_atoms_changed')

    const startedAt = performance.now()
    const outcome = publishShape(source, ['P one.', ...paragraphs(2_000), '## H'].join('\n\n'))
    const elapsed = performance.now() - startedAt

    expect(outcome.error?.reason).toBe('unsupported_suggestion_structure')
    expect(outcome.error?.suggestionReason).toBe('too_many_new_blocks')
    expect(outcome.unchanged).toBe(true)
    // Turning one break per block into suggestions grows with the square of
    // their number (measured: more than three times the parse cost at this
    // size); the refusal must come before that work, not after it.
    expect(elapsed).toBeLessThan(baselineElapsed * 2 + 100)

    const justAbove = ['P one.', ...paragraphs(MAX_PROPOSED_BLOCK_BREAKS + 1), '## H'].join('\n\n')
    expect(publishShape(source, justAbove).error?.suggestionReason).toBe('too_many_new_blocks')

    const limitStartedAt = performance.now()
    expectPublishedShape(publishShape(
      source,
      ['P one.', ...paragraphs(MAX_PROPOSED_BLOCK_BREAKS), '## H'].join('\n\n'),
    ))
    expect(performance.now() - limitStartedAt).toBeLessThan(5_000)
  }, 30_000)
})

describe('whole words in a published suggestion', () => {
  it.each([
    ['a one-letter change', 'Die Kosten steigen.', 'Die Kostet steigen.', 'Kosten', 'Kostet'],
    ['a changed amount', 'Budget 18.000 Euro.', 'Budget 21.500 Euro.', '18.000', '21.500'],
    ['a changed decimal', 'Frist 1,5 Jahre.', 'Frist 2,5 Jahre.', '1,5', '2,5'],
    ['an extended ending', 'Das Haus steht.', 'Das Hauses steht.', 'Haus', 'Hauses'],
    ['an inserted word', 'Das Haus steht.', 'Das neue Haus steht.', '', 'neue '],
    ['a deleted word', 'Das neue Haus steht.', 'Das Haus steht.', 'neue ', ''],
  ])('marks %s as whole words', (_case, source, target, deleted, inserted) => {
    const outcome = publishShape(source, target)

    expectPublishedShape(outcome)
    expect(markedText(outcome.json, 'deletion')).toBe(deleted)
    expect(markedText(outcome.json, 'insertion')).toBe(inserted)
  })

  it('keeps the change minimal in a word that holds another writer\'s pending suggestion', () => {
    const plain = schema.nodeFromJSON(parseEditorMarkdown('Die Kosten steigen.'))
    const state = EditorState.create({ schema, doc: plain })
    // "Die Kosten" ends at 11: the other writer appends "s" to the word.
    const tracked = transformToInqtrixSuggestionTransaction(
      state.tr.insertText('s', 11),
      state,
      {
        authorId: OTHER_USER_ID,
        createdAt: CREATED_AT - 10,
        patchId: EXISTING_PATCH_ID,
      },
      () => EXISTING_SUGGESTION_ID,
    ).doc
    const document = editorJsonToYDoc(tracked.toJSON())

    publishTargetSuggestion(document, {
      actorUserId: USER_ID,
      patchId: PATCH_ID,
      ...markdownTarget('Die kostens steigen.'),
    }, {
      createSuggestionId: () => SUGGESTION_ID,
      nowMilliseconds: () => CREATED_AT,
    })

    const json = editorYDocToJson(document)
    expect(serializeEditorJson(json, 'final').trim()).toBe('Die kostens steigen.')
    expect(JSON.stringify(json)).toContain(EXISTING_SUGGESTION_ID)
    expect(markedText(json, 'deletion')).toBe('K')
    expect(markedText(json, 'insertion')).toBe('k')
  })
})

describe('suggestion timestamps', () => {
  afterEach(() => {
    vi.useRealTimers()
  })

  // A typed suggestion is stamped in epoch milliseconds by the browser; the
  // publisher stamped seconds, so one document held both units and every
  // reader had to guess. Published suggestions now use the same unit.
  it('stamps a published suggestion with the current time in epoch milliseconds', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-10-02T12:03:29.250Z'))
    const document = editorJsonToYDoc(parseEditorMarkdown('Hello'))

    const result = publishTargetSuggestion(document, {
      actorUserId: USER_ID,
      patchId: PATCH_ID,
      ...markdownTarget('Hello world'),
    }, { createSuggestionId: () => SUGGESTION_ID })

    expect(result.suggestions.map((descriptor) => descriptor.createdAt))
      .toEqual([Date.parse('2026-10-02T12:03:29.250Z')])
  })
})

type JSONContentLike = {
  content?: JSONContentLike[]
  marks?: unknown[]
  text?: string
  type: string
}

type ShapeOutcome = {
  beforeJson: ReturnType<typeof editorYDocToJson>
  document: Y.Doc
  error: CollaborationError | null
  json: ReturnType<typeof editorYDocToJson>
  patchIds: string[]
  policy: () => void
  source: string
  target: string
  unchanged: boolean
}

/** Publish `target` onto `source` and keep everything a shape check needs. */
function publishShape(
  source: string | ReturnType<typeof parseEditorMarkdown>,
  target: string,
): ShapeOutcome {
  const document = editorJsonToYDoc(
    typeof source === 'string' ? parseEditorMarkdown(source) : source,
  )
  const beforeJson = editorYDocToJson(document)
  const beforeDocument = new Y.Doc()
  Y.applyUpdate(beforeDocument, Y.encodeStateAsUpdate(document))
  const vector = Y.encodeStateVector(document)
  let next = 0
  let error: CollaborationError | null = null
  let patchIds: string[] = []
  try {
    patchIds = publishTargetSuggestion(document, {
      actorUserId: USER_ID,
      patchId: PATCH_ID,
      ...markdownTarget(target),
    }, {
      createSuggestionId: () => `33333333-3333-4333-8333-${String(++next).padStart(12, '0')}`,
      nowMilliseconds: () => CREATED_AT,
    }).patchIds
  } catch (caught) {
    if (!(caught instanceof CollaborationError)) throw caught
    error = caught
  }
  const json = editorYDocToJson(document)
  return {
    beforeJson,
    document,
    error,
    json,
    patchIds,
    policy: () => {
      validateSuggestionUpdate(beforeJson, json, 'suggest', USER_ID, {
        afterDocument: document,
        beforeDocument,
        deletedByUpdate: Y.decodeUpdate(Y.encodeStateAsUpdate(document, vector)).ds,
      })
    },
    source: serializeEditorJson(beforeJson, 'final').trim(),
    target,
    unchanged: JSON.stringify(json) === JSON.stringify(beforeJson),
  }
}

/** A published shape: original is the source, final is the target, accepting
 * gives the target without anchors, rejecting restores the source exactly,
 * and the result is a valid document a human suggest-mode update could
 * also have produced. */
function expectPublishedShape(outcome: ShapeOutcome, label = outcome.target): void {
  expect(outcome.error, label).toBeNull()
  const canonicalTarget = serializeEditorJson(parseEditorMarkdown(outcome.target), 'final').trim()
  expect(serializeEditorJson(outcome.json, 'original').trim(), label).toBe(outcome.source)
  expect(serializeEditorJson(outcome.json, 'final').trim(), label).toBe(canonicalTarget)

  const accepted = editorJsonToYDoc(outcome.json)
  resolvePatchDecision(accepted, { decision: 'accept', patchIds: outcome.patchIds })
  const acceptedJson = editorYDocToJson(accepted)
  expect(serializeEditorJson(acceptedJson, 'final').trim(), label).toBe(canonicalTarget)
  expect(JSON.stringify(acceptedJson), label).not.toContain('​')

  const rejected = editorJsonToYDoc(outcome.json)
  resolvePatchDecision(rejected, { decision: 'reject', patchIds: outcome.patchIds })
  expect(editorYDocToJson(rejected), label).toEqual(outcome.beforeJson)

  expect(() => validateDocument(outcome.document, 4_000_000), label).not.toThrow()
  expect(outcome.policy, label).not.toThrow()
}

/** A publication without a base, compared with the document as it is. */
function markdownTarget(markdown: string): { base: null; target: ReturnType<typeof parseRoundTrippableTarget> } {
  return { base: null, target: parseRoundTrippableTarget(markdown) }
}

/** The text this publication (PATCH_ID) marks as deleted or inserted, in
 * document order: what its review card shows. */
function markedText(
  json: ReturnType<typeof editorYDocToJson>,
  kind: 'deletion' | 'insertion',
): string {
  let text = ''
  schema.nodeFromJSON(json).descendants((node) => {
    if (node.isText && node.marks.some((mark) => (
      mark.type.name === kind && mark.attrs.patchId === PATCH_ID
    ))) text += node.text
  })
  return text
}

function refusalOf(publish: () => unknown): CollaborationError | null {
  try {
    publish()
  } catch (caught) {
    if (caught instanceof CollaborationError) return caught
    throw caught
  }
  return null
}
