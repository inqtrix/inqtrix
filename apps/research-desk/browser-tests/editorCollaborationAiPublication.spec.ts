import { editorYDocToJson } from '@inqtrix/editor-schema'
import { expect, test, type Browser } from '@playwright/test'
import type { JSONContent } from '@tiptap/core'

import {
  CollaborationStack,
  WRITER_A,
  WRITER_B,
  closeWriters,
  editorText,
  expectSaved,
  observedNotices,
  openWriter,
  placeCaretAfter,
  placeCaretAtBlockEnd,
  suggestionsOf,
  typeSlowly,
  watchNotices,
  writerDocument,
  writerText,
  type AiSuggestionSeed,
  type FixtureWriter,
  type WriterPage,
} from './collaborationStack'

// Accepting a private AI suggestion publishes it into the shared document:
// a durability barrier, the projection it is built on, then the publication
// request. Another writer who types in between moves the shared document on.
// Each test holds the publication request until what the other writer typed
// after the click has reached the accepting page, so the race the timing of
// a live session decides is the same in every run.

const SHARED = [
  '# Abschnitt A',
  '',
  'Der Assistent ersetzt hier ein Wort.',
  '',
  'Notizen B:',
  '',
  '## Abschnitt C',
  '',
  'Notizen C:',
].join('\n')
const SEED_TEXT = 'Der Assistent ersetzt'
const SUGGESTION: AiSuggestionSeed = {
  after: ' hier ein Wort.',
  before: 'Der Assistent ',
  find: 'ersetzt',
  replace: 'tauscht',
}
const AI_PARAGRAPH = 1
const NOTES_B = 2

type PublicationRoute = {
  bodies: Array<Record<string, unknown>>
  /** Resolves with the first publication request once it is held. */
  held: Promise<Record<string, unknown>>
  /** Projections the page asked for through the barrier. */
  projections: number
  release: () => void
  statuses: number[]
}

/**
 * Answers the two API endpoints of the publication path for the accepting
 * page from the stack's service, and holds the first publication request
 * until `release`.
 */
async function servePublication(
  page: WriterPage,
  stack: CollaborationStack,
  documentId: string,
): Promise<PublicationRoute> {
  let release = (): void => undefined
  const released = new Promise<void>((resolve) => {
    release = resolve
  })
  let hold: (body: Record<string, unknown>) => void = () => undefined
  const held = new Promise<Record<string, unknown>>((resolve) => {
    hold = resolve
  })
  const route: PublicationRoute = { bodies: [], held, projections: 0, release: () => release(), statuses: [] }
  await page.page.route('**/v1/editor/documents/*/collaboration/projection:flush', async (request) => {
    route.projections += 1
    await request.fulfill({ json: await stack.browserProjection(documentId) })
  })
  await page.page.route('**/v1/editor/documents/*/suggestions:publish', async (request) => {
    const body = request.request().postDataJSON() as Record<string, unknown>
    route.bodies.push(body)
    if (route.bodies.length === 1) {
      hold(body)
      await released
    }
    const answer = await stack.browserPublication(documentId, page.writer, body)
    route.statuses.push(answer.status)
    await request.fulfill({ json: answer.json, status: answer.status })
  })
  return route
}

async function openPair(
  browser: Browser,
  stack: CollaborationStack,
  documentId: string,
): Promise<{ owner: WriterPage; typist: WriterPage }> {
  const owner = await openWriter(browser, stack, WRITER_A, {
    aiSuggestion: SUGGESTION,
    documentId,
    seedText: SEED_TEXT,
  })
  const typist = await openWriter(browser, stack, WRITER_B, { documentId, seedText: SEED_TEXT })
  return { owner, typist }
}

/** The suggestions of one author in a page's live document. */
async function suggestionsBy(writer: WriterPage, author: FixtureWriter): Promise<string[]> {
  const document = await writerDocument(writer)
  try {
    return suggestionsOf(document).filter((entry) => entry.endsWith(`:${author.id}`))
  } finally {
    document.destroy()
  }
}

/** The text `author`'s suggestions of `kind` mark in the writer's document,
 * in document order: what the tracked change shows. */
async function markedTextBy(
  writer: WriterPage,
  author: FixtureWriter,
  kind: 'deletion' | 'insertion',
): Promise<string> {
  const document = await writerDocument(writer)
  try {
    let text = ''
    const visit = (node: JSONContent): void => {
      if (node.type === 'text' && node.marks?.some((mark) => (
        mark.type === kind && mark.attrs?.authorId === author.id
      ))) text += node.text ?? ''
      node.content?.forEach(visit)
    }
    visit(editorYDocToJson(document))
    return text
  } finally {
    document.destroy()
  }
}

function occurrences(text: string, marker: string): number {
  return text.split(marker).length - 1
}

test('an AI suggestion accepted while another writer types is published on the first click, and what the other writer typed stays exactly once', async ({ browser }, testInfo) => {
  test.setTimeout(120_000)
  const documentId = 'ai-accept-while-typing-doc'
  const stack = await CollaborationStack.start({ documents: [{ id: documentId, markdown: SHARED }] })
  const writers: WriterPage[] = []
  try {
    const { owner, typist } = await openPair(browser, stack, documentId)
    writers.push(owner, typist)
    const route = await servePublication(owner, stack, documentId)
    await owner.page.getByTestId('ai-seed').click()
    const changes = owner.page.getByTestId('ai-changes')
    await expect(changes).toContainText('tauscht')
    await watchNotices(owner)
    await watchNotices(typist)
    await placeCaretAtBlockEnd(typist, NOTES_B)

    await changes.getByRole('button', { name: 'Alle übernehmen' }).click()
    const held = await route.held
    // Typed after the click, while the publication is on its way.
    await typeSlowly(typist, ' MARKE1', 110)
    await expectSaved(typist)
    await expect.poll(() => editorText(owner)).toContain('Notizen B: MARKE1')
    expect(stack.storedUpdateCount(documentId)).toBeGreaterThan(held.expected_sequence as number)
    route.release()

    await expect.poll(() => route.statuses).toEqual([200])
    await expect(owner.page.getByTestId('ai-statuses')).toHaveText('accepted')
    expect(route.bodies).toHaveLength(1)
    expect(route.projections).toBe(1)
    expect(String(held.base_markdown)).toContain('Der Assistent ersetzt hier ein Wort.')
    expect(String(held.base_markdown)).not.toContain('MARKE1')

    // The tracked change shows in both sessions, the typed text once.
    const final = SHARED.replace('ersetzt', 'tauscht').replace('Notizen B:', 'Notizen B: MARKE1')
    const original = SHARED.replace('Notizen B:', 'Notizen B: MARKE1')
    for (const writer of [owner, typist]) {
      await expect.poll(() => writerText(writer, 'final')).toBe(final)
      expect(await writerText(writer, 'original')).toBe(original)
      expect(await suggestionsBy(writer, WRITER_A)).toHaveLength(2)
      // The tracked change marks the changed word whole, not 'ersetz' to
      // 'tausch'.
      expect(await markedTextBy(writer, WRITER_A, 'deletion')).toBe('ersetzt')
      expect(await markedTextBy(writer, WRITER_A, 'insertion')).toBe('tauscht')
      expect(occurrences(await editorText(writer), 'MARKE1')).toBe(1)
    }
    expect(stack.storeText(documentId, 'final')).toBe(final)
    expect(stack.storeText(documentId, 'original')).toBe(original)
    expect(stack.storedRows(documentId).at(-1)?.input).toMatchObject({
      actorKind: 'assistant',
      actorUserId: WRITER_A.id,
      changeKind: 'suggestion',
    })

    await expect(owner.page.locator('[data-suggestion-error]')).toHaveCount(0)
    for (const writer of [owner, typist]) {
      const notices = await observedNotices(writer)
      expect(notices.editorChanges).toBeGreaterThan(0)
      expect(notices.shown).toEqual([])
      expect(writer.consoleErrors).toEqual([])
      await expect(writer.state).toHaveAttribute('data-connection-status', 'connected')
    }
    await owner.page.screenshot({ path: testInfo.outputPath('ai-accepted-while-typing.png') })
  } finally {
    await closeWriters(writers)
    await stack.stop()
  }
})

test('an AI suggestion whose passage another writer changes while it is accepted ends calmly as changed, and nothing is published', async ({ browser }, testInfo) => {
  test.setTimeout(120_000)
  const documentId = 'ai-accept-passage-changed-doc'
  const stack = await CollaborationStack.start({ documents: [{ id: documentId, markdown: SHARED }] })
  const writers: WriterPage[] = []
  try {
    const { owner, typist } = await openPair(browser, stack, documentId)
    writers.push(owner, typist)
    const route = await servePublication(owner, stack, documentId)
    await owner.page.getByTestId('ai-seed').click()
    const changes = owner.page.getByTestId('ai-changes')
    await expect(changes).toContainText('tauscht')
    await placeCaretAfter(typist, AI_PARAGRAPH, 'Der Assistent er', 'setzt hier ein Wort.')

    await changes.getByRole('button', { name: 'Alle übernehmen' }).click()
    await route.held
    // Typed into the very word the AI rewrites.
    await typeSlowly(typist, 'X', 110)
    await expectSaved(typist)
    await expect.poll(() => editorText(owner)).toContain('Der Assistent erXsetzt hier ein Wort.')
    route.release()

    await expect(owner.page.getByTestId('ai-statuses')).toHaveText('stale')
    const card = changes.locator('[data-suggestion-stale]')
    await expect(card).toHaveText('Textstelle hat sich geändert. Bitte Vorschlag neu erzeugen.')
    await expect(card).toHaveAttribute('role', 'status')
    await expect(owner.page.locator('[data-suggestion-error]')).toHaveCount(0)
    await expect(owner.page.locator('[role="alert"]')).toHaveCount(0)
    // One refused publication, built again once on the current projection,
    // where the passage no longer resolves: nothing is sent a second time.
    expect(route.statuses).toEqual([409])
    expect(route.projections).toBe(2)

    const changed = SHARED.replace('ersetzt', 'erXsetzt')
    expect(stack.storeText(documentId, 'final')).toBe(changed)
    expect(stack.storeText(documentId, 'original')).toBe(changed)
    expect(stack.storedRows(documentId).every((row) => row.input.actorKind !== 'assistant')).toBe(true)
    // The refused request is the one console line: the browser reports
    // every answer with an error status as a resource that failed to load.
    const refusedLoads = owner.consoleErrors.filter((message) => /\b409\b/.test(message))
    expect(refusedLoads.length).toBeLessThanOrEqual(1)
    expect(owner.consoleErrors.filter((message) => !refusedLoads.includes(message))).toEqual([])
    expect(typist.consoleErrors).toEqual([])
    for (const writer of [owner, typist]) {
      await expect.poll(() => writerText(writer, 'final')).toBe(changed)
      expect(await suggestionsBy(writer, WRITER_A)).toEqual([])
      await expect(writer.state).toHaveAttribute('data-connection-status', 'connected')
      await expect(writer.state).toHaveAttribute('data-can-edit', 'true')
    }
    for (const transport of owner.transports) expect(transport.closes).toEqual([])
    await owner.page.screenshot({ path: testInfo.outputPath('ai-accept-passage-changed.png') })
  } finally {
    await closeWriters(writers)
    await stack.stop()
  }
})

// Rejecting a private AI suggestion withdraws its draft, the authorisation
// to publish it. The row the suggestion hangs on goes with it when it is
// the machine carrier of an assistant run, and stays when it is a note the
// user wrote. A carrier is no note: it never marks the text.

const SECOND_SUGGESTION = { after2: ' C:', before2: '', find2: 'Notizen', replace2: 'Notiz' }
const STALE_TEXT = 'Textstelle hat sich geändert. Bitte Vorschlag neu erzeugen.'
const CARRIER_MARKS = '.editor-comment-anchor[data-editor-comment-kind="assistant_edit"]'
const NOTE_MARKS = '.editor-comment-anchor[data-editor-comment-kind="inline_edit"]'

/** Runs `body` on the owner page of a fresh shared document whose AI run
 * proposes `aiSuggestion`, and checks that the page logged no error. */
async function onOwner(
  browser: Browser,
  documentId: string,
  aiSuggestion: AiSuggestionSeed,
  body: (owner: WriterPage) => Promise<void>,
): Promise<void> {
  const stack = await CollaborationStack.start({ documents: [{ id: documentId, markdown: SHARED }] })
  const writers: WriterPage[] = []
  try {
    const owner = await openWriter(browser, stack, WRITER_A, { aiSuggestion, documentId, seedText: SEED_TEXT })
    writers.push(owner)
    await body(owner)
    expect(owner.consoleErrors).toEqual([])
  } finally {
    await closeWriters(writers)
    await stack.stop()
  }
}

/** Answers the deletions of a suggestion's rows and lists them in order, as
 * `draft:<comment id>` or `row:<comment id>`. */
async function recordRowDeletions(writer: WriterPage): Promise<string[]> {
  const deletions: string[] = []
  await writer.page.route('**/v1/editor/documents/*/comments/**', async (route) => {
    const request = route.request()
    if (request.method() !== 'DELETE') {
      await route.fallback()
      return
    }
    const path = new URL(request.url()).pathname
    const draft = path.endsWith('/suggestion-draft')
    const commentId = path.split('/comments/')[1]!.replace(/\/suggestion-draft$/, '')
    deletions.push(`${draft ? 'draft' : 'row'}:${commentId}`)
    await route.fulfill({ body: '', status: 204 })
  })
  return deletions
}

/** Seeds the page's AI suggestions and waits until the rows they hang on,
 * listed as `commentIds`, are in the page's state. */
async function seedSuggestions(owner: WriterPage, commentIds: string): Promise<void> {
  await owner.page.getByTestId('ai-seed').click()
  await expect(owner.page.getByTestId('ai-changes')).toContainText('tauscht')
  await expect(owner.page.getByTestId('ai-comments')).toHaveText(commentIds)
}

test('rejecting an AI suggestion in a shared document deletes its carrier row', async ({ browser }) => {
  test.setTimeout(120_000)
  await onOwner(browser, 'ai-reject-carrier-doc', { ...SUGGESTION, origin: 'carrier' }, async (owner) => {
    const deletions = await recordRowDeletions(owner)
    await seedSuggestions(owner, 'ai-publication-comment-1')
    const reject = owner.page.getByTestId('ai-changes').getByRole('button', { exact: true, name: 'Verwerfen' })

    await reject.click()
    // The card goes once the reject has run to its end.
    await expect(reject).toHaveCount(0)
    // The draft first, which withdraws the authorisation, then the row.
    expect(deletions).toEqual(['draft:ai-publication-comment-1', 'row:ai-publication-comment-1'])
    await expect(owner.page.getByTestId('ai-comments')).toHaveText('')
  })
})

test('a carrier never marks the text, a note does', async ({ browser }) => {
  test.setTimeout(120_000)
  const aiSuggestion: AiSuggestionSeed = { ...SUGGESTION, ...SECOND_SUGGESTION, origin: 'carrier', origin2: 'note' }
  await onOwner(browser, 'ai-carrier-unmarked-doc', aiSuggestion, async (owner) => {
    await seedSuggestions(owner, 'ai-publication-comment-1,ai-publication-comment-2')
    // Both rows reach the editor surface together: once the note's mark
    // shows, the carrier's would show as well.
    await expect(owner.page.locator(NOTE_MARKS)).not.toHaveCount(0)
    await expect(owner.page.locator(CARRIER_MARKS)).toHaveCount(0)
  })
})

test('rejecting an AI suggestion of a note keeps the note and its mark', async ({ browser }) => {
  test.setTimeout(120_000)
  await onOwner(browser, 'ai-reject-note-doc', { ...SUGGESTION, origin: 'note' }, async (owner) => {
    const deletions = await recordRowDeletions(owner)
    await seedSuggestions(owner, 'ai-publication-comment-1')
    const noteMarks = owner.page.locator(NOTE_MARKS)
    await expect(noteMarks).not.toHaveCount(0)

    await owner.page.getByTestId('ai-changes').getByRole('button', { exact: true, name: 'Verwerfen' }).click()
    await expect(owner.page.getByTestId('ai-statuses')).toHaveText('rejected')
    // Only the suggestion's draft goes; the note is the user's.
    expect(deletions).toEqual(['draft:ai-publication-comment-1'])
    await expect(owner.page.getByTestId('ai-comments')).toHaveText('ai-publication-comment-1')
    await expect(noteMarks).not.toHaveCount(0)
  })
})

test('a stale AI suggestion is rejected by its own button, and its carrier row goes with it', async ({ browser }) => {
  test.setTimeout(120_000)
  await onOwner(browser, 'ai-reject-stale-doc', { ...SUGGESTION, origin: 'carrier' }, async (owner) => {
    const deletions = await recordRowDeletions(owner)
    await seedSuggestions(owner, 'ai-publication-comment-1')
    const changes = owner.page.getByTestId('ai-changes')
    await owner.page.getByTestId('ai-mark-stale').click()
    await expect(owner.page.getByTestId('ai-statuses')).toHaveText('stale')
    await expect(changes.locator('[data-suggestion-stale]')).toHaveText(STALE_TEXT)
    const reject = changes.getByRole('button', { exact: true, name: 'Verwerfen' })

    await reject.click()
    await expect(reject).toHaveCount(0)
    expect(deletions).toEqual(['draft:ai-publication-comment-1', 'row:ai-publication-comment-1'])
    await expect(owner.page.getByTestId('ai-comments')).toHaveText('')
  })
})

test('rejecting all AI suggestions also rejects stale ones, with every carrier row', async ({ browser }) => {
  test.setTimeout(120_000)
  const aiSuggestion: AiSuggestionSeed = { ...SUGGESTION, ...SECOND_SUGGESTION, origin: 'carrier', origin2: 'carrier' }
  await onOwner(browser, 'ai-reject-all-stale-doc', aiSuggestion, async (owner) => {
    const deletions = await recordRowDeletions(owner)
    await seedSuggestions(owner, 'ai-publication-comment-1,ai-publication-comment-2')
    const statuses = owner.page.getByTestId('ai-statuses')
    const markStale = owner.page.getByTestId('ai-mark-stale')
    await markStale.click()
    await expect(statuses).toHaveText('stale,pending')
    await markStale.click()
    await expect(statuses).toHaveText('stale,stale')
    const changes = owner.page.getByTestId('ai-changes')
    const rejectAll = changes.getByRole('button', { exact: true, name: 'Alle verwerfen' })

    await expect(rejectAll).toBeEnabled()
    await rejectAll.click()
    await expect(changes.getByRole('button', { exact: true, name: 'Verwerfen' })).toHaveCount(0)
    // Each suggestion's draft before its row; both rows go.
    for (const commentId of ['ai-publication-comment-1', 'ai-publication-comment-2']) {
      const draft = deletions.indexOf(`draft:${commentId}`)
      expect(draft, commentId).toBeGreaterThanOrEqual(0)
      expect(deletions.indexOf(`row:${commentId}`), commentId).toBeGreaterThan(draft)
    }
    expect(deletions).toHaveLength(4)
    await expect(owner.page.getByTestId('ai-comments')).toHaveText('')
  })
})
