import { afterEach, describe, expect, it, vi } from 'vitest'

import type { EditorCommentAnchorRecord, EditorSuggestionRecord } from '@/features/project/types'

import { LlmSuggestionProducer } from './suggestionProducer'

const ANCHOR: EditorCommentAnchorRecord = {
  from: 10,
  quoteAfter: '. Bei Fragen hilft der Support weiter.',
  quoteBefore: 'Nach der Änderung muss die Sitzung neu gestartet werden, ',
  selectedText: 'das alte Passwort danach nicht mehr gilt',
  to: 50,
}

function stubSuggestEndpoint() {
  const bodies: Array<Record<string, unknown>> = []
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    if (String(input).includes('/v1/editor/suggest')) {
      bodies.push(JSON.parse(String(init?.body ?? '{}')) as Record<string, unknown>)
    }
    return new Response(JSON.stringify({
      change_summary: [],
      improved_text: 'Das alte Passwort gilt danach nicht mehr',
      warnings: [],
    }), { headers: { 'Content-Type': 'application/json' }, status: 200 })
  }))
  return bodies
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('the passage is sent with the text around it', () => {
  // The model saw only the whole document as background, not where the
  // passage sits in its sentence; a rewrite of a passage that ends before the
  // full stop then brought its own full stop and the document read "..".
  it('sends the anchor text before and after the passage with a first suggestion', async () => {
    const bodies = stubSuggestEndpoint()
    const producer = new LlmSuggestionProducer({ locale: 'de', stack: 'default' })

    await producer.produce({
      anchor: ANCHOR,
      documentId: 'doc-1',
      documentMarkdown: '# Bericht',
      instruction: 'Diesen Teil als eigenen kurzen Satz formulieren.',
      modelTier: null,
      origin: { commentId: 'comment-1', kind: 'inline_edit' },
      originalText: ANCHOR.selectedText,
    })

    expect(bodies).toHaveLength(1)
    expect(bodies[0]?.context_before).toBe(ANCHOR.quoteBefore)
    expect(bodies[0]?.context_after).toBe(ANCHOR.quoteAfter)
  })

  it('sends the same neighbours when a suggestion is refined', async () => {
    const bodies = stubSuggestEndpoint()
    const producer = new LlmSuggestionProducer({ locale: 'de', stack: 'default' })
    const suggestion = {
      anchor: ANCHOR,
      originalText: ANCHOR.selectedText,
      proposedText: 'Das alte Passwort gilt danach nicht mehr.',
    } as EditorSuggestionRecord

    await producer.refine({
      documentMarkdown: '# Bericht',
      instruction: 'Noch knapper.',
      modelTier: null,
      suggestion,
    })

    expect(bodies).toHaveLength(1)
    expect(bodies[0]?.context_before).toBe(ANCHOR.quoteBefore)
    expect(bodies[0]?.context_after).toBe(ANCHOR.quoteAfter)
  })

  // A suggestion of a document-wide instruction can insert a block of its own
  // before or after its anchor's block. Its text never meets the anchor's
  // neighbours, so they are no seam: sent along, the server framed the new
  // block as the passage of that sentence and dropped its closing full stop
  // whenever the anchor ended right before one.
  it('sends no neighbours when the refined suggestion inserts a block of its own', async () => {
    for (const editPosition of ['after', 'before', 'append'] as const) {
      const bodies = stubSuggestEndpoint()
      const producer = new LlmSuggestionProducer({ locale: 'de', stack: 'default' })
      const suggestion = {
        anchor: ANCHOR,
        editPosition,
        originalText: '',
        proposedText: 'Ein neuer Absatz für alle Fragen.',
      } as EditorSuggestionRecord

      await producer.refine({
        documentMarkdown: '# Bericht',
        instruction: 'Noch knapper.',
        modelTier: null,
        suggestion,
      })

      expect(bodies, editPosition).toHaveLength(1)
      expect(bodies[0], editPosition).not.toHaveProperty('context_before')
      expect(bodies[0], editPosition).not.toHaveProperty('context_after')
      vi.unstubAllGlobals()
    }

    // A suggestion that replaces its anchor keeps them.
    const bodies = stubSuggestEndpoint()
    await new LlmSuggestionProducer({ locale: 'de', stack: 'default' }).refine({
      documentMarkdown: '# Bericht',
      instruction: 'Noch knapper.',
      modelTier: null,
      suggestion: {
        anchor: ANCHOR,
        editPosition: 'replace',
        originalText: ANCHOR.selectedText,
        proposedText: 'Das alte Passwort gilt danach nicht mehr.',
      } as EditorSuggestionRecord,
    })
    expect(bodies[0]?.context_after).toBe(ANCHOR.quoteAfter)
  })
})
