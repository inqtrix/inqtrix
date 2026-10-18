import { getSchema } from '@tiptap/core'
import { initProseMirrorDoc, updateYFragment } from '@tiptap/y-tiptap'
import {
  EDITOR_YJS_FRAGMENT,
  createEditorSchemaExtensions,
  editorYDocToJson,
  getEditorSchemaFingerprint,
  parseEditorMarkdown,
  serializeEditorJson,
} from '@inqtrix/editor-schema'
import * as Y from 'yjs'

import type { LoadedDocumentState } from '../src/contracts'
import {
  hashBytes,
  reconstructDocument,
  reconstructValidatedDocument,
  sameBytes,
} from '../src/documentState'
import { documentState, markdownDocument } from './helpers'

const schema = getSchema(createEditorSchemaExtensions({ enableUndoRedo: false }))

describe('sameBytes', () => {
  // This comparison guards cache reuse against a stale authoritative
  // state, so its semantics are a safety contract. The implementation
  // may get faster; what it answers may not change.
  it('accepts only byte-for-byte identical buffers', () => {
    expect(sameBytes(new Uint8Array([1, 2, 3]), new Uint8Array([1, 2, 3]))).toBe(true)
    expect(sameBytes(new Uint8Array([]), new Uint8Array([]))).toBe(true)
  })

  it('rejects a difference in any position, including the last byte', () => {
    expect(sameBytes(new Uint8Array([1, 2, 3]), new Uint8Array([1, 2, 4]))).toBe(false)
    expect(sameBytes(new Uint8Array([9, 2, 3]), new Uint8Array([1, 2, 3]))).toBe(false)
  })

  it('rejects differing lengths even when one is a prefix of the other', () => {
    // A prefix must never pass: a truncated state that happens to match
    // the start of the authoritative one is exactly the case this guard
    // exists for.
    expect(sameBytes(new Uint8Array([1, 2]), new Uint8Array([1, 2, 3]))).toBe(false)
    expect(sameBytes(new Uint8Array([1, 2, 3]), new Uint8Array([1, 2]))).toBe(false)
  })

  it('compares content, not the backing buffer offset', () => {
    // Yjs hands out views into pooled ArrayBuffers. Comparing the
    // underlying buffer instead of the view would silently report
    // equality for two different states that share one allocation.
    const backing = new Uint8Array([7, 1, 2, 3, 9])
    const view = backing.subarray(1, 4)

    expect(sameBytes(view, new Uint8Array([1, 2, 3]))).toBe(true)
    expect(sameBytes(view, new Uint8Array([7, 1, 2]))).toBe(false)
  })
})

describe('document reconstruction', () => {
  it('returns one reusable immutable validation of the reconstructed state', async () => {
    const expected = markdownDocument('Reusable state')
    const state = await documentState('ed_test', expected)

    const validated = await reconstructValidatedDocument(state, {
      documentId: 'ed_test',
      generation: 1,
      schemaVersion: 2,
    }, 1024 * 1024)

    expect(hashBytes(validated.encodedState)).toBe(validated.stateHash)
    expect(validated.canonicalJson).toEqual(editorYDocToJson(validated.document))
    expect(Object.isFrozen(validated.canonicalJson)).toBe(true)
    expect(Object.isFrozen(validated.canonicalJson.content)).toBe(true)
    validated.document.destroy()
    expected.destroy()
  })

  it('keeps the legacy single-snapshot response compatible', async () => {
    const expected = markdownDocument('Legacy snapshot')
    const state = await documentState('ed_test', expected)

    const reconstructed = await reconstructDocument(state, {
      documentId: 'ed_test',
      generation: 1,
      schemaVersion: 2,
    }, 1024 * 1024)

    expect(markdown(reconstructed)).toBe('Legacy snapshot')
    reconstructed.destroy()
    expected.destroy()
  })

  it('recovers from a corrupt newest snapshot using the older candidate and its tail', async () => {
    const older = markdownDocument('Hello')
    const current = new Y.Doc()
    Y.applyUpdate(current, Y.encodeStateAsUpdate(older))
    const currentVector = Y.encodeStateVector(current)
    const fragment = current.getXmlFragment(EDITOR_YJS_FRAGMENT)
    const initialized = initProseMirrorDoc(fragment, schema)
    updateYFragment(
      current,
      fragment,
      schema.nodeFromJSON(parseEditorMarkdown('Hello!')),
      initialized.meta,
    )
    const tail = Y.encodeStateAsUpdate(current, currentVector)
    const olderState = Y.encodeStateAsUpdate(older)
    const corrupt = new Uint8Array([1, 2, 3])
    const rejected = vi.fn()
    const state: LoadedDocumentState = {
      documentId: 'ed_test',
      generation: 1,
      persistedSequence: 1,
      schemaHash: await getEditorSchemaFingerprint(),
      schemaVersion: 2,
      snapshot: {
        coveredSequence: 1,
        stateHash: hashBytes(corrupt),
        stateUpdate: corrupt,
        stateVector: new Uint8Array(),
      },
      snapshotCandidates: [
        {
          snapshot: {
            coveredSequence: 1,
            stateHash: hashBytes(corrupt),
            stateUpdate: corrupt,
            stateVector: new Uint8Array(),
          },
          updates: [],
        },
        {
          snapshot: {
            coveredSequence: 0,
            stateHash: hashBytes(olderState),
            stateUpdate: olderState,
            stateVector: Y.encodeStateVector(older),
          },
          updates: [{ hash: hashBytes(tail), sequence: 1, update: tail }],
        },
      ],
      updates: [],
    }

    const reconstructed = await reconstructDocument(state, {
      documentId: 'ed_test',
      generation: 1,
      schemaVersion: 2,
    }, 1024 * 1024, { onCandidateRejected: rejected })

    expect(markdown(reconstructed)).toBe('Hello!')
    expect(rejected).toHaveBeenCalledWith({
      candidateIndex: 0,
      reason: 'internal_consistency',
    })
    reconstructed.destroy()
    current.destroy()
    older.destroy()
  })

  it('falls back when the newest snapshot is valid but its tail hash is corrupt', async () => {
    const fixture = await candidateFixture()
    fixture.state.snapshotCandidates![0]!.updates[0]!.hash = '0'.repeat(64)
    const rejected = vi.fn()

    const reconstructed = await reconstructDocument(fixture.state, {
      documentId: 'ed_test',
      generation: 1,
      schemaVersion: 2,
    }, 1024 * 1024, { onCandidateRejected: rejected })

    expect(markdown(reconstructed)).toBe('Hello!!')
    expect(rejected).toHaveBeenCalledWith({
      candidateIndex: 0,
      reason: 'internal_consistency',
    })
    reconstructed.destroy()
    fixture.destroy()
  })

  it.each(['corrupt', 'incomplete'] as const)(
    'fails loudly when all snapshot candidates have %s tails',
    async (failure) => {
      const fixture = await candidateFixture()
      if (failure === 'corrupt') {
        fixture.state.snapshotCandidates![0]!.updates[0]!.hash = '0'.repeat(64)
        fixture.state.snapshotCandidates![1]!.updates[1]!.hash = '0'.repeat(64)
      } else {
        fixture.state.snapshotCandidates![0]!.updates = []
        fixture.state.snapshotCandidates![1]!.updates.pop()
      }

      await expect(reconstructDocument(fixture.state, {
        documentId: 'ed_test',
        generation: 1,
        schemaVersion: 2,
      }, 1024 * 1024)).rejects.toThrowError('internal_consistency')
      fixture.destroy()
    },
  )
})

async function candidateFixture(): Promise<{
  destroy: () => void
  state: LoadedDocumentState
}> {
  const base = markdownDocument('Hello')
  const current = new Y.Doc()
  Y.applyUpdate(current, Y.encodeStateAsUpdate(base))
  const first = advance(current, 'Hello!')
  const newestSnapshot = Y.encodeStateAsUpdate(current)
  const newestVector = Y.encodeStateVector(current)
  const second = advance(current, 'Hello!!')
  const baseSnapshot = Y.encodeStateAsUpdate(base)
  const newest = {
    coveredSequence: 1,
    stateHash: hashBytes(newestSnapshot),
    stateUpdate: newestSnapshot,
    stateVector: newestVector,
  }
  const state: LoadedDocumentState = {
    documentId: 'ed_test',
    generation: 1,
    persistedSequence: 2,
    schemaHash: await getEditorSchemaFingerprint(),
    schemaVersion: 2,
    snapshot: newest,
    snapshotCandidates: [
      {
        snapshot: newest,
        updates: [{ hash: hashBytes(second), sequence: 2, update: second }],
      },
      {
        snapshot: {
          coveredSequence: 0,
          stateHash: hashBytes(baseSnapshot),
          stateUpdate: baseSnapshot,
          stateVector: Y.encodeStateVector(base),
        },
        updates: [
          { hash: hashBytes(first), sequence: 1, update: first },
          { hash: hashBytes(second), sequence: 2, update: second },
        ],
      },
    ],
    updates: [{ hash: hashBytes(second), sequence: 2, update: second }],
  }
  return {
    destroy: () => {
      current.destroy()
      base.destroy()
    },
    state,
  }
}

function advance(document: Y.Doc, targetMarkdown: string): Uint8Array {
  const vector = Y.encodeStateVector(document)
  const fragment = document.getXmlFragment(EDITOR_YJS_FRAGMENT)
  const initialized = initProseMirrorDoc(fragment, schema)
  updateYFragment(
    document,
    fragment,
    schema.nodeFromJSON(parseEditorMarkdown(targetMarkdown)),
    initialized.meta,
  )
  return Y.encodeStateAsUpdate(document, vector)
}

function markdown(document: Y.Doc): string {
  return serializeEditorJson(editorYDocToJson(document), 'final').trim()
}
