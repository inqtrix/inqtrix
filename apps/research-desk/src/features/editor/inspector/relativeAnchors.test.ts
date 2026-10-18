import type { EditorRelativePositionAdapter } from '@inqtrix/editor-schema'
import { describe, expect, it } from 'vitest'

import type { EditorCommentAnchorRecord } from '@/features/project/types'
import {
  hasCollaborationRelativeAnchor,
  resolveCollaborationAnchor,
  serializeCollaborationAnchor,
} from './relativeAnchors'

const anchor: EditorCommentAnchorRecord = {
  from: 4,
  quoteAfter: 'after',
  quoteBefore: 'before',
  selectedText: 'selected',
  to: 12,
}

describe('collaboration relative anchors', () => {
  it('serializes both boundaries while preserving quote fallback context', () => {
    const adapter: EditorRelativePositionAdapter = {
      fromProseMirrorPosition: (position) => `relative-${position}`,
      toProseMirrorPosition: () => null,
    }

    const serialized = serializeCollaborationAnchor(anchor, adapter)

    expect(serialized).toMatchObject({
      ...anchor,
      relativeFrom: 'relative-4',
      relativeTo: 'relative-12',
      relativeVersion: 'yjs-relative-position-base64-v1',
    })
    expect(hasCollaborationRelativeAnchor(serialized)).toBe(true)
  })

  it('prefers a valid relative range and falls back to the untouched quote anchor', () => {
    const serialized = serializeCollaborationAnchor(anchor, {
      fromProseMirrorPosition: (position) => String(position),
      toProseMirrorPosition: () => null,
    })
    const resolved = resolveCollaborationAnchor(serialized, {
      fromProseMirrorPosition: () => '',
      toProseMirrorPosition: (position) => position === '4' ? 9 : 17,
    })
    expect(resolved).toMatchObject({ source: 'relative', anchor: { from: 9, to: 17 } })

    const fallback = resolveCollaborationAnchor(serialized, {
      fromProseMirrorPosition: () => '',
      toProseMirrorPosition: () => null,
    })
    expect(fallback).toEqual({ anchor: serialized, source: 'quote' })
  })

  it('leaves legacy absolute anchors unchanged', () => {
    const result = resolveCollaborationAnchor(anchor, {
      fromProseMirrorPosition: () => '',
      toProseMirrorPosition: () => 99,
    })
    expect(result).toEqual({ anchor, source: 'quote' })
  })
})
