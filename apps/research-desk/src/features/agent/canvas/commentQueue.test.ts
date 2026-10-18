import { describe, expect, it } from 'vitest'

import {
  anchorFromMarkdownQuote,
  canvasContextFromQueue,
  canvasContextFromSelection,
  settleCanvasQueueAfterSubmit,
  type AgentCanvasCommentDraft,
  pinAfterComment,
} from './commentQueue'

const draft = (comment: string): AgentCanvasCommentDraft => ({
  artifactId: 'art_1',
  comment,
  id: `ui-${comment}`,
  plainText: 'Satz',
  quote: 'Der Umsatz stieg.',
  quoteAfter: '',
  quoteBefore: '',
  revision: 3,
})

describe('anchorFromMarkdownQuote', () => {
  it('cuts 80-character context windows around the located quote', () => {
    const before = 'a'.repeat(120)
    const after = 'b'.repeat(120)
    const markdown = `${before}KERN${after}`
    const anchor = anchorFromMarkdownQuote(markdown, 'KERN')
    expect(anchor.quote).toBe('KERN')
    expect(anchor.quoteBefore).toBe('a'.repeat(80))
    expect(anchor.quoteAfter).toBe('b'.repeat(80))
  })

  it('clamps the windows at the document edges', () => {
    const anchor = anchorFromMarkdownQuote('KERN danach', 'KERN')
    expect(anchor.quoteBefore).toBe('')
    expect(anchor.quoteAfter).toBe(' danach')
  })

  it('keeps the quote with empty contexts when the source lacks it', () => {
    expect(anchorFromMarkdownQuote('anderer Text', 'KERN')).toEqual({
      quote: 'KERN',
      quoteAfter: '',
      quoteBefore: '',
    })
  })
})

describe('canvasContextFromQueue', () => {
  it('attaches nothing for an empty queue', () => {
    expect(canvasContextFromQueue([])).toBeUndefined()
  })

  it('binds the open document to the first queued comment', () => {
    const context = canvasContextFromQueue([draft('eins'), draft('zwei')])
    expect(context).toMatchObject({ artifactId: 'art_1', revision: 3 })
    expect(context?.comments.map((item) => item.comment)).toEqual([
      'eins',
      'zwei',
    ])
  })
})

describe('settleCanvasQueueAfterSubmit', () => {
  it('empties the queue ONLY on an accepted submission', () => {
    const queue = [draft('eins')]
    expect(settleCanvasQueueAfterSubmit(queue, true)).toEqual([])
    // Rejected submit: every queued comment survives for the retry.
    expect(settleCanvasQueueAfterSubmit(queue, false)).toBe(queue)
  })
})

describe('canvasContextFromSelection (single-document channel)', () => {
  const draft = {
    artifactId: 'art_queue',
    comment: 'Bitte schaerfen.',
    id: 'ui-selection-draft',
    plainText: 'Satz',
    quote: 'Satz',
    quoteAfter: '',
    quoteBefore: '',
    revision: 3,
  }

  it('lets queued comments own the channel over a mention pin', () => {
    const context = canvasContextFromSelection(
      [draft],
      { artifactId: 'art_pin', revision: 7 },
    )
    expect(context).toMatchObject({ artifactId: 'art_queue', revision: 3 })
  })

  it('sends a comment-less pin when the queue is empty', () => {
    const context = canvasContextFromSelection(
      [],
      { artifactId: 'art_pin', revision: 7 },
    )
    expect(context).toEqual({
      artifactId: 'art_pin',
      comments: [],
      revision: 7,
    })
  })

  it('attaches nothing without queue or pin', () => {
    expect(canvasContextFromSelection([], null)).toBeUndefined()
  })
})

describe('a queued comment and the pinned canvas document', () => {
  // Comments bind the single-document channel to THEIR document. A pin on
  // that same document is no conflict and stays; a pin on another one ends
  // (its capsule stays in the sentence as the plain word the user typed).
  it('keeps a pin on the comment’s own document', () => {
    expect(pinAfterComment('a1', 'a1')).toBe('a1')
  })

  it('ends a pin on another document, and leaves no pin alone', () => {
    expect(pinAfterComment('a1', 'a2')).toBeNull()
    expect(pinAfterComment(null, 'a2')).toBeNull()
  })
})
