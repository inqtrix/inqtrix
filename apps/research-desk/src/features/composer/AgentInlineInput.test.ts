import { readFileSync } from 'node:fs'

import { describe, expect, it } from 'vitest'

import { pillKindsKeptAfterSend, pillKindsKeptByAnswer } from '@/features/agent/contextCategories'

import { CommandPill } from './CommandPill'
import {
  commandPillNode,
  composerTextFromDoc,
  docKeepingPills,
  pillsFromDoc,
} from './AgentInlineInput'

describe('the pill reconcile survives a swap', () => {
  // Switching the response form from canvas to chat in the run-setup menu
  // removes one pill and inserts another. Doing both in ONE chain throws
  // "Position N out of range": the insertion point is then read from the
  // document before the deletion shortens it. Behavioural tests cannot see
  // this, so this suite pins the ordering in the source instead of the
  // resulting ProseMirror positions.
  const source = readFileSync(
    'src/features/composer/AgentInlineInput.tsx',
    'utf8',
  )

  it('deletes and inserts in separate transactions', () => {
    // Anchored on the effect's own bounds: `useImperativeHandle` first
    // appears in the import line, so indexOf() on it sliced backwards and
    // produced an empty string that passed nothing.
    const reconcile = source.slice(
      source.indexOf('const wanted = new Map('),
      source.lastIndexOf('useImperativeHandle(ref'),
    )
    expect(reconcile.length).toBeGreaterThan(200)
    expect(reconcile).toContain('if (stale.length > 0) {')
    // The insertion point is read AFTER the deletions, inside its own chain.
    const insertAt = reconcile.indexOf('editor.state.doc.content.size')
    const deleteRun = reconcile.indexOf('chain.run()')
    expect(deleteRun).toBeGreaterThan(-1)
    expect(insertAt).toBeGreaterThan(deleteRun)
  })

  it('deletes back to front so earlier positions stay valid', () => {
    expect(source).toContain('for (const { pos, word } of stale.reverse())')
  })
})

describe('the reconcile does not report itself back', () => {
  const source = readFileSync(
    'src/features/composer/AgentInlineInput.tsx',
    'utf8',
  )

  // Switching canvas → chat deletes the old pill before inserting the new
  // one. That deletion fires `onUpdate` with an empty pill set; taken at face
  // value the composer reads it as "the user removed it" and resets the form
  // to auto, swallowing the very choice that started the reconcile, so the
  // new pill is never wanted.
  it('suppresses the edit report while it rewrites the document', () => {
    expect(source).toContain('syncingRef')
    expect(source).toContain('if (!syncingRef.current && !sameSet(current, reportedRef.current))')
    expect(source).toContain('syncingRef.current = true')
    // Cleared in a finally, so a throwing transaction cannot leave the
    // component deaf to real user edits.
    expect(source).toContain('} finally {\n        syncingRef.current = false')
  })
})

describe('what a capsule contributes to the text', () => {
  // A capsule holds a document position but no character, so reading the bare
  // text would submit the sentence with a hole where the capsule stood
  // ("gib die Antwort in  und nutze") and the transcript would show a question
  // the user never wrote. The sent text spells the label; the effect itself
  // still travels in the request fields.
  // Both pills were TYPED as words of the sentence (`/canvas`, `/wissen`),
  // built by the same constructor the `/`-command uses.
  const doc = {
    content: [{
      content: [
        { text: 'gib die Antwort in ', type: 'text' },
        commandPillNode({ id: 'canvas', kind: 'responseForm', label: 'Canvas' }, true),
        { text: ' und nutze ', type: 'text' },
        commandPillNode({ id: '', kind: 'knowledge', label: 'Wissen' }, true),
      ],
      type: 'paragraph',
    }],
    type: 'doc',
  }

  it('spells the label in the text that is SENT', () => {
    expect(composerTextFromDoc(doc, true))
      .toBe('gib die Antwort in Canvas und nutze Wissen')
  })

  it('leaves the label out of the LIVE value', () => {
    // The draft round-trips through plain text: a label here would be written
    // back as words and grow a second capsule beside the one the switches
    // already produce.
    expect(composerTextFromDoc(doc)).toBe('gib die Antwort in  und nutze ')
    expect(composerTextFromDoc(doc)).not.toContain('Canvas')
  })

  it('keeps a hard break a newline in both projections', () => {
    const broken = {
      content: [{
        content: [{ text: 'a', type: 'text' }, { type: 'hardBreak' }, { text: 'b', type: 'text' }],
        type: 'paragraph',
      }],
      type: 'doc',
    }
    expect(composerTextFromDoc(broken)).toBe('a\nb')
    expect(composerTextFromDoc(broken, true)).toBe('a\nb')
  })
})

describe('a pill a switch placed never becomes question text', () => {
  // The run-setup menu (and a restored session) inserts its pill at the end
  // of the document at that moment; with an empty composer that is the start,
  // and whatever the user then types follows it. Spelled out, the label was
  // sent as part of the question and glued to the first word:
  // "CanvasErstelle aus den Unterlagen ...".
  const setting = (kind: 'knowledge' | 'responseForm' | 'skill', id: string, label: string) => (
    commandPillNode({ id, kind, label }, false)
  )
  const paragraph = (...content: object[]) => ({
    content: [{ content, type: 'paragraph' }],
    type: 'doc',
  })

  it('sends exactly what the user typed after a menu pill', () => {
    const doc = paragraph(
      setting('responseForm', 'canvas', 'Canvas'),
      { text: 'Erstelle aus den Unterlagen ein Memo.', type: 'text' },
    )
    expect(composerTextFromDoc(doc, true)).toBe('Erstelle aus den Unterlagen ein Memo.')
  })
})

describe('a setting pill leaves a word boundary, never a word', () => {
  const setting = (kind: 'knowledge' | 'responseForm' | 'skill', id: string, label: string) => (
    commandPillNode({ id, kind, label }, false)
  )
  const lines = (...paragraphs: object[][]) => ({
    content: paragraphs.map((content) => ({ content, type: 'paragraph' })),
    type: 'doc',
  })

  it('drops every kind of setting at the start, the middle and the end', () => {
    const doc = lines([
      setting('knowledge', '', 'Wissen'),
      setting('skill', 'memo', 'Memo-Vorlage'),
      { text: 'Erstelle ein Memo', type: 'text' },
      setting('responseForm', 'canvas', 'Canvas'),
      { text: 'über die Lage', type: 'text' },
      setting('responseForm', 'chat', 'Chat'),
    ])
    // No label anywhere, and the words around the middle one are not glued.
    expect(composerTextFromDoc(doc, true)).toBe('Erstelle ein Memo über die Lage')
  })

  it('touches no whitespace away from a setting and keeps its lines', () => {
    const doc = lines(
      [{ text: 'Erste  Zeile', type: 'text' }],
      [setting('knowledge', '', 'Wissen'), { text: ' zweite Zeile', type: 'text' }],
    )
    expect(composerTextFromDoc(doc, true)).toBe('Erste  Zeile\nzweite Zeile')
  })

  it('keeps a typed word beside a setting', () => {
    const doc = lines([
      setting('responseForm', 'canvas', 'Canvas'),
      { text: 'nutze ', type: 'text' },
      commandPillNode({ id: '', kind: 'knowledge', label: 'Wissen' }, true),
    ])
    expect(composerTextFromDoc(doc, true)).toBe('nutze Wissen')
  })
})

describe('each way into the document says who placed the pill', () => {
  // The editor is not mounted in this node environment, so the two
  // insertion paths are pinned in the source - the same way this file
  // pins the reconcile's transaction order.
  const source = readFileSync('src/features/composer/AgentInlineInput.tsx', 'utf8')

  it('marks a pill the `/`-command replaced in place as typed', () => {
    const typed = source.slice(
      source.indexOf('replaceTokenWithPill: (tokenLength, pill) => {'),
      source.indexOf('setText: (text) => {'),
    )
    expect(typed.length).toBeGreaterThan(100)
    expect(typed).toContain('commandPillNode(pill, true)')
  })

  it('marks a pill a switch placed as a setting', () => {
    const reconcile = source.slice(
      source.indexOf('const wanted = new Map('),
      source.lastIndexOf('useImperativeHandle(ref'),
    )
    expect(reconcile).toContain('commandPillNode(pill, false)')
    expect(reconcile).not.toContain('commandPillNode(pill, true)')
  })
})

describe('the typed flag survives HTML', () => {
  // The attribute default would render `false` as the string "false" - which
  // is true - so a copied setting pill would come back as a typed word.
  const attributes = CommandPill.config.addAttributes?.call({} as never) as Record<string, {
    parseHTML: (element: { getAttribute: (name: string) => string | null }) => boolean
    renderHTML: (attributes: Record<string, unknown>) => Record<string, string>
  }>

  it('renders only a typed pill as typed', () => {
    expect(attributes.pillTyped.renderHTML({ pillTyped: false })).toEqual({})
    expect(attributes.pillTyped.renderHTML({ pillTyped: true })).toEqual({ 'data-pill-typed': 'true' })
  })

  it('reads a pill back as typed only from an explicit true', () => {
    const element = (value: string | null) => ({ getAttribute: () => value })
    expect(attributes.pillTyped.parseHTML(element('true'))).toBe(true)
    expect(attributes.pillTyped.parseHTML(element('false'))).toBe(false)
    expect(attributes.pillTyped.parseHTML(element(null))).toBe(false)
  })
})

describe('an answer to a clarification holds the rule pills back', () => {
  // A rule picked before the question arrived holds for the next TASK. An
  // answer starts none: spelled into the answer and cleared with it, the
  // rule would have vanished without ever applying to anything.
  const doc = {
    content: [{
      content: [
        { text: 'Ja, im Stil ', type: 'text' },
        commandPillNode({ id: 'pt_brief', kind: 'rule', label: 'Executive Briefing' }, true),
        { text: ' und mit ', type: 'text' },
        commandPillNode({ id: 'memo', kind: 'skill', label: 'memo' }, true),
      ],
      type: 'paragraph',
    }],
    type: 'doc',
  }

  it('leaves the rule title out of the answer text', () => {
    expect(composerTextFromDoc(doc, true, pillKindsKeptByAnswer(true)))
      .toBe('Ja, im Stil und mit memo')
    // A new task spells the rule as the word the user typed.
    expect(composerTextFromDoc(doc, true, pillKindsKeptByAnswer(false)))
      .toBe('Ja, im Stil Executive Briefing und mit memo')
  })

  it('keeps the rule pills in the cleared draft, and nothing else', () => {
    const kept = docKeepingPills(doc, pillKindsKeptByAnswer(true))
    expect(pillsFromDoc(kept)).toEqual([
      { id: 'pt_brief', kind: 'rule', label: 'Executive Briefing' },
    ])
    // Kept as a setting: not a word of the next task's sentence.
    expect(composerTextFromDoc(kept, true)).toBe('')
    // A new task clears every pill, as before.
    expect(pillsFromDoc(docKeepingPills(doc, pillKindsKeptByAnswer(false)))).toEqual([])
  })
})

describe('a document capsule names its document in the sent question', () => {
  // A document is the material of the task, not a setting of it: whichever
  // way it entered the draft ("@" in the sentence or "+" as a capsule at its
  // end), the question names it, in the quotes of the reader's language.
  const quote = (label: string) => `„${label}“`
  const doc = {
    content: [{
      content: [
        { text: 'Vergleiche ', type: 'text' },
        commandPillNode({ id: 'r1', kind: 'report', label: 'CO2-Bericht' }, true),
        { text: ' mit ', type: 'text' },
        commandPillNode({ id: 'r2', kind: 'report', label: 'Marktbericht' }, false),
        { text: ' und überarbeite ', type: 'text' },
        commandPillNode({ id: 'd1', kind: 'editTarget', label: 'Vertragsentwurf' }, true),
        commandPillNode({ id: 'canvas', kind: 'responseForm', label: 'Canvas' }, false),
      ],
      type: 'paragraph',
    }],
    type: 'doc',
  }

  it('quotes a typed and a placed document capsule and keeps a setting out', () => {
    expect(composerTextFromDoc(doc, true, [], quote))
      .toBe('Vergleiche „CO2-Bericht“ mit „Marktbericht“ und überarbeite „Vertragsentwurf“')
  })

  it('leaves the live draft text free of every capsule', () => {
    expect(composerTextFromDoc(doc)).toBe('Vergleiche  mit  und überarbeite ')
  })

  it('holds document capsules out of an answer and keeps them for the next task', () => {
    const answerKinds = pillKindsKeptByAnswer(true)
    expect(composerTextFromDoc(doc, true, answerKinds, quote)).toBe('Vergleiche mit und überarbeite')
    expect(pillsFromDoc(docKeepingPills(doc, answerKinds)).map((pill) => pill.kind))
      .toEqual(['report', 'report', 'editTarget'])
  })

  it('keeps the editing target after a task, as a setting at the start', () => {
    const kept = docKeepingPills(doc, pillKindsKeptAfterSend(false))
    expect(pillsFromDoc(kept)).toEqual([{ id: 'd1', kind: 'editTarget', label: 'Vertragsentwurf' }])
    // Still a document: the next question names it.
    expect(composerTextFromDoc(kept, true, [], quote)).toBe('„Vertragsentwurf“')
  })
})

describe('a document the user typed outlives its attachment as a word', () => {
  // When a document's attachment ends from outside (a comment on another
  // canvas document ends the pin), deleting its capsule would delete a word
  // of the user's sentence. The editor is not mounted in this node
  // environment, so the reconcile is pinned in the source.
  const source = readFileSync('src/features/composer/AgentInlineInput.tsx', 'utf8')
  const reconcile = source.slice(
    source.indexOf('const wanted = new Map('),
    source.lastIndexOf('useImperativeHandle(ref'),
  )

  it('turns a typed document capsule into its plain word and drops anything else', () => {
    expect(reconcile).toContain('node.attrs.pillTyped === true')
    expect(reconcile).toContain('DOCUMENT_PILL_KINDS.includes(node.attrs.pillKind as CommandPillKind)')
    expect(reconcile).toContain('stale.push({ pos, word: keepsWord ? String(node.attrs.pillLabel) : null })')
    expect(reconcile).toContain('if (word) chain.insertContentAt({ from: pos, to: pos + 1 }, word)')
    expect(reconcile).toContain('else chain.deleteRange({ from: pos, to: pos + 1 })')
  })
})

describe('a document placed by a switch keeps a word boundary on both sides', () => {
  // "+" places the capsule at the end of the draft, right after the last
  // typed word: spelled without a boundary it glued to it ("mit„Bericht“").
  const quote = (label: string) => `„${label}“`
  const placed = (label: string) => commandPillNode({ id: label, kind: 'report', label }, false)

  it('spells it as a word of its own', () => {
    const doc = {
      content: [{
        content: [{ text: 'Vergleiche kurz mit', type: 'text' }, placed('Marktbericht'), { text: 'bitte', type: 'text' }],
        type: 'paragraph',
      }],
      type: 'doc',
    }
    expect(composerTextFromDoc(doc, true, [], quote)).toBe('Vergleiche kurz mit „Marktbericht“ bitte')
  })

  it('adds no space at the edges of a line', () => {
    const doc = { content: [{ content: [placed('Marktbericht')], type: 'paragraph' }], type: 'doc' }
    expect(composerTextFromDoc(doc, true, [], quote)).toBe('„Marktbericht“')
  })
})
