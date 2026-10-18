import {
  createEditorSchemaExtensions,
  parseEditorMarkdown,
  serializeEditorJson,
} from '@inqtrix/editor-schema'
import { Editor as HeadlessEditor } from '@tiptap/core'
import type { Editor } from '@tiptap/react'
import { describe, expect, it } from 'vitest'

import type { EditorSuggestionRecord } from '@/features/project/types'
import { applySuggestionToEditor } from './useEditorSuggestions'

const SATZ = 'Vergleichstest ohne Zusammenarbeit. Dieser Absatz dient als Ausgangstext fuer eine einzelne Ersetzung.'

function editorMit(markdown: string): Editor {
  return new HeadlessEditor({
    content: parseEditorMarkdown(markdown),
    element: null,
    extensions: createEditorSchemaExtensions({ enableUndoRedo: false }),
    injectCSS: false,
  }) as unknown as Editor
}

/** Vorschlag auf den Inline-Bereich von `wort` im Satz. */
function ersetzung(quelle: string, wort: string, ersatz: string): EditorSuggestionRecord {
  const index = quelle.indexOf(wort)
  return {
    // +1: ProseMirror-Positionen zaehlen den oeffnenden Absatzknoten mit.
    anchor: {
      from: index + 1,
      quoteAfter: quelle.slice(index + wort.length, index + wort.length + 6),
      quoteBefore: quelle.slice(Math.max(0, index - 6), index),
      selectedMarkdown: wort,
      selectedText: wort,
      to: index + wort.length + 1,
    },
    anchorText: wort,
    blockId: 'block-1',
    createdAt: '2026-08-24T09:00:00.000Z',
    documentId: 'editor-doc-1',
    groupId: 'group-1',
    id: 'suggestion-1',
    originalMarkdown: wort,
    originalText: wort,
    origin: { kind: 'global_run' },
    proposedText: ersatz,
    status: 'pending',
    updatedAt: '2026-08-24T09:00:00.000Z',
  }
}

describe('applySuggestionToEditor', () => {
  // Die inline-erhaltende Entscheidung existierte bereits genau einmal,
  // wurde aber nur vom Kollaborationszweig benutzt. Der lokale Zweig gab
  // den Vorschlag als Markdown-STRING an insertContentAt und erzeugte
  // damit einen Block: aus einem Satz wurden drei Absaetze, server-
  // bestaetigt im gespeicherten content_markdown.
  it('haelt eine Wortersetzung im selben Absatz', () => {
    const editor = editorMit(SATZ)

    expect(applySuggestionToEditor(editor, ersetzung(SATZ, 'Ausgangstext', 'Basistext'))).toBe(true)

    // (a) mechanisch: ein Block. Genuegt ALLEIN nicht.
    expect(editor.getJSON().content?.length).toBe(1)
    // (b) inhaltlich: der Satz liest sich als EIN korrekter Satz mit dem
    //     ersetzten Wort an der richtigen Stelle. Das ist das Kriterium.
    expect(serializeEditorJson(editor.getJSON(), 'final').trim()).toBe(
      'Vergleichstest ohne Zusammenarbeit. Dieser Absatz dient als Basistext fuer eine einzelne Ersetzung.',
    )
  })

  it('laesst einen MEHRBLOCKIGEN Vorschlag weiterhin als Bloecke landen', () => {
    // Gegenprobe gegen die naheliegende Uebertreibung "immer inline":
    // ein Vorschlag aus mehreren Absaetzen muss Absaetze bleiben, sonst
    // zerstoert der Helfer beim naechsten Anfassen Absatzvorschlaege.
    const editor = editorMit(SATZ)
    const mehrblockig = ersetzung(SATZ, 'Ausgangstext', 'Erster Absatz.\n\nZweiter Absatz.')

    expect(applySuggestionToEditor(editor, mehrblockig)).toBe(true)

    const bloecke = editor.getJSON().content ?? []
    expect(bloecke.length).toBeGreaterThan(1)
    const markdown = serializeEditorJson(editor.getJSON(), 'final')
    expect(markdown).toContain('Erster Absatz.')
    expect(markdown).toContain('Zweiter Absatz.')
  })
})
