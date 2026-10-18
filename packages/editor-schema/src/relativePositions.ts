import {
  absolutePositionToRelativePosition,
  relativePositionToAbsolutePosition,
} from '@tiptap/y-tiptap'
import type { Node as ProseMirrorNode } from '@tiptap/pm/model'
import * as Y from 'yjs'

export type SerializedRelativePosition = string
export type ProseMirrorMapping = Map<
  Y.AbstractType<unknown>,
  ProseMirrorNode | ProseMirrorNode[]
>

export type EditorRelativePositionAdapter = {
  fromProseMirrorPosition(position: number): SerializedRelativePosition
  toProseMirrorPosition(position: SerializedRelativePosition): number | null
}

export function createRelativePositionAdapter(
  document: Y.Doc,
  fragment: Y.XmlFragment,
  mapping: ProseMirrorMapping,
): EditorRelativePositionAdapter {
  if (fragment.doc !== document) {
    throw new Error('Relative-position fragment must belong to the supplied Y.Doc')
  }
  return {
    fromProseMirrorPosition(position) {
      return proseMirrorPositionToRelativePosition(position, fragment, mapping)
    },
    toProseMirrorPosition(position) {
      return relativePositionToProseMirrorPosition(document, fragment, position, mapping)
    },
  }
}

export function serializeRelativePosition(position: Y.RelativePosition): SerializedRelativePosition {
  return bytesToBase64(Y.encodeRelativePosition(position))
}

export function deserializeRelativePosition(value: SerializedRelativePosition): Y.RelativePosition {
  const bytes = base64ToBytes(value)
  if (bytes.byteLength > 512) {
    throw new Error('Relative position payload is too large')
  }
  return Y.decodeRelativePosition(bytes)
}

export function proseMirrorPositionToRelativePosition(
  position: number,
  fragment: Y.XmlFragment,
  mapping: ProseMirrorMapping,
): SerializedRelativePosition {
  if (!Number.isSafeInteger(position) || position < 0) {
    throw new Error('ProseMirror position must be a non-negative integer')
  }
  const relative = absolutePositionToRelativePosition(position, fragment, mapping)
  return serializeRelativePosition(relative)
}

export function relativePositionToProseMirrorPosition(
  document: Y.Doc,
  fragment: Y.XmlFragment,
  value: SerializedRelativePosition,
  mapping: ProseMirrorMapping,
): number | null {
  return relativePositionToAbsolutePosition(
    document,
    fragment,
    deserializeRelativePosition(value),
    mapping,
  )
}

function bytesToBase64(bytes: Uint8Array): string {
  let binary = ''
  for (const byte of bytes) binary += String.fromCharCode(byte)
  return btoa(binary)
}

function base64ToBytes(value: string): Uint8Array {
  if (!/^[A-Za-z0-9+/]*={0,2}$/.test(value) || value.length % 4 !== 0) {
    throw new Error('Invalid relative position encoding')
  }
  const binary = atob(value)
  return Uint8Array.from(binary, (character) => character.charCodeAt(0))
}
