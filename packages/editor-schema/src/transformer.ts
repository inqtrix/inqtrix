import { ProsemirrorTransformer } from '@hocuspocus/transformer'
import type { JSONContent } from '@tiptap/core'
import { getSchema } from '@tiptap/core'
import * as Y from 'yjs'
import { EDITOR_YJS_FRAGMENT } from './constants.js'
import { createEditorSchemaExtensions } from './extensions.js'
import { isStructureSuggestionData } from './structureSuggestions.js'
import { assertYjsCompatibleSuggestionStructure } from './suggestions.js'
import {
  EDITOR_YJS_TEXTBLOCK_NAMES,
  completeTextblocksCreatedSince,
} from './textblockPlaceholder.js'

const EDITOR_YJS_SCHEMA = getSchema(
  createEditorSchemaExtensions({ enableUndoRedo: false }),
)

/**
 * Builds the shared state of `content`. Every textblock without content holds
 * its one empty text node, as every other writer of a new textblock writes it
 * (see textblockPlaceholder.ts).
 */
export function editorJsonToYDoc(content: JSONContent): Y.Doc {
  const document = EDITOR_YJS_SCHEMA.nodeFromJSON(content)
  assertYjsCompatibleSuggestionStructure(document)
  document.check()
  const built = ProsemirrorTransformer.toYdoc(
    content,
    EDITOR_YJS_FRAGMENT,
    EDITOR_YJS_SCHEMA,
  )
  completeTextblocksCreatedSince(built, 0)
  return built
}

export function editorYDocToJson(document: Y.Doc): JSONContent {
  assertCanonicalEditorYDocRoots(document)
  const content = ProsemirrorTransformer.fromYdoc(
    document,
    EDITOR_YJS_FRAGMENT,
  ) as JSONContent
  const editorDocument = EDITOR_YJS_SCHEMA.nodeFromJSON(content)
  editorDocument.check()
  return editorDocument.toJSON()
}

function assertCanonicalEditorYDocRoots(document: Y.Doc): void {
  const rootNames = [...document.share.keys()]
  if (rootNames.length !== 1 || rootNames[0] !== EDITOR_YJS_FRAGMENT) {
    throw new Error(
      `Yjs document must contain only the ${EDITOR_YJS_FRAGMENT} shared root`,
    )
  }
  let contentRoot: Y.XmlFragment
  // Yjs updates omit top-level constructors, so a received XmlFragment starts
  // as AbstractType. Keyed root data must still be rejected after materializing it.
  try {
    contentRoot = document.getXmlFragment(EDITOR_YJS_FRAGMENT)
  } catch {
    throw new Error(`Yjs shared root ${EDITOR_YJS_FRAGMENT} must be an XmlFragment`)
  }
  if (!(document.share.get(EDITOR_YJS_FRAGMENT) instanceof Y.XmlFragment)) {
    throw new Error(`Yjs shared root ${EDITOR_YJS_FRAGMENT} could not be materialized`)
  }
  if (
    hasKeyedXmlFragmentState(contentRoot)
    || contentRoot.toArray().some((child) => !(child instanceof Y.XmlElement))
  ) {
    throw new Error(`Yjs shared root ${EDITOR_YJS_FRAGMENT} has a non-canonical structure`)
  }
}

function hasKeyedXmlFragmentState(fragment: Y.XmlFragment): boolean {
  // Yjs has no public XmlFragment API for keyed state. Keep this version-pinned
  // compatibility read isolated; raw client updates are validated without it.
  const keyedState: unknown = Reflect.get(fragment, '_map')
  return keyedState instanceof Map && keyedState.size > 0
}

export function validateEditorYDoc(document: Y.Doc): JSONContent {
  assertCausallyCompleteYDoc(document)
  const content = editorYDocToJson(document)
  const canonical = editorJsonToYDoc(content)
  try {
    const canonicalJson = editorYDocToJson(canonical)
    if (
      JSON.stringify(content) !== JSON.stringify(canonicalJson)
      || !sameCanonicalYjsStructure(document, canonical)
    ) {
      throw new Error('Yjs document is not canonical for the Inqtrix editor schema')
    }
    return content
  } finally {
    canonical.destroy()
  }
}

/**
 * Requires a complete, unambiguous Yjs V1 update encoding.
 *
 * Valid Yjs transaction updates are not guaranteed to be byte-identical after
 * `mergeUpdates()` re-encodes them (notably when one transaction combines
 * structure attributes and deletions). Decode through Yjs' public V1 reader
 * instead and require the reader to consume the complete payload. This keeps
 * trailing bytes and V2 payloads rejected without rejecting valid client
 * transactions merely because Yjs chooses another equivalent encoding.
 */
export function validateCanonicalYjsV1Update(update: Uint8Array): Uint8Array {
  const document = new Y.Doc()
  let consumedBytes = -1
  try {
    const decoder = {
      arr: update,
      pos: 0,
    } as Parameters<typeof Y.readUpdate>[0]
    Y.readUpdate(decoder, document)
    consumedBytes = decoder.pos
  } catch {
    throw new Error('Client update is not a valid canonical Yjs V1 update')
  } finally {
    document.destroy()
  }
  if (consumedBytes !== update.byteLength) {
    throw new Error('Client update is not a fully consumed canonical Yjs V1 update')
  }
  return update
}

/**
 * Validates the raw operation vocabulary accepted from suggestion clients.
 *
 * This is intentionally reject-only: the returned bytes are the exact bytes
 * that were inspected and may cross the persistence and broadcast boundary.
 */
export function validateSuggestionYjsUpdate(update: Uint8Array): Uint8Array {
  let decoded: ReturnType<typeof Y.decodeUpdate>
  try {
    decoded = Y.decodeUpdate(update)
  } catch {
    throw new Error('Suggestion update is not a valid Yjs V1 update')
  }

  const inserted = new Map<number, Array<{ from: number; to: number }>>()
  for (const struct of decoded.structs) {
    if (!(struct instanceof Y.Item)) {
      throw new Error('Suggestion update contains a non-canonical Yjs struct')
    }
    const ranges = inserted.get(struct.id.client) ?? []
    ranges.push({ from: struct.id.clock, to: struct.id.clock + struct.length })
    inserted.set(struct.id.client, ranges)

    if (typeof struct.parent === 'string' && struct.parent !== EDITOR_YJS_FRAGMENT) {
      throw new Error('Suggestion update contains a non-canonical Yjs parent')
    }
    if (struct.parentSub !== null) {
      if (
        struct.content instanceof Y.ContentAny
        && EDITOR_YJS_NODE_ATTRIBUTE_NAMES.has(struct.parentSub)
      ) continue
      throw new Error('Suggestion update contains a non-canonical Yjs attribute')
    }
    if (struct.content instanceof Y.ContentString) continue
    // Replacing an existing Y.Map/XmlElement attribute is encoded relative to
    // the previous item. In that valid form Yjs omits `parentSub`, so the raw
    // update alone cannot recover the attribute key. Admit only the one
    // bounded object vocabulary that Suggest mode writes this way; the
    // materialized-document validation immediately afterwards proves that it
    // actually resolves to the canonical structure-suggestion attribute.
    if (
      struct.content instanceof Y.ContentAny
      && struct.content.getContent().length === 1
      && isStructureSuggestionData(struct.content.getContent()[0])
    ) continue
    if (
      struct.content instanceof Y.ContentFormat
      && EDITOR_YJS_FORMAT_NAMES.has(struct.content.key)
    ) continue
    if (
      struct.content instanceof Y.ContentType
      && struct.content.type instanceof Y.XmlText
    ) continue
    if (
      struct.content instanceof Y.ContentType
      && struct.content.type instanceof Y.XmlElement
      && EDITOR_YJS_NODE_NAMES.has(struct.content.type.nodeName)
    ) continue
    throw new Error(
      `Suggestion update contains non-canonical Yjs content: ${struct.content.constructor.name}`,
    )
  }

  for (const [client, ranges] of decoded.ds.clients) {
    const insertedRanges = inserted.get(client)
    if (!insertedRanges) continue
    for (const deleted of ranges) {
      const deletedTo = deleted.clock + deleted.len
      if (insertedRanges.some((range) => (
        range.from < deletedTo && deleted.clock < range.to
      ))) {
        throw new Error('Suggestion update contains transient Yjs history')
      }
    }
  }
  return update
}

/**
 * Proves that a materialized document has no unresolved struct or delete-set
 * dependencies using only Yjs' encoded update and state-vector APIs.
 */
export function assertCausallyCompleteYDoc(document: Y.Doc): void {
  const stateVector = Y.decodeStateVector(Y.encodeStateVector(document))
  const encoded = Y.decodeUpdate(Y.encodeStateAsUpdate(document))
  for (const struct of encoded.structs) {
    const integratedClock = stateVector.get(struct.id.client) ?? 0
    if (struct.id.clock + struct.length > integratedClock) {
      throw new Error('Yjs document contains unresolved struct dependencies')
    }
  }
  for (const [client, ranges] of encoded.ds.clients) {
    const integratedClock = stateVector.get(client) ?? 0
    if (ranges.some((range) => range.clock + range.len > integratedClock)) {
      throw new Error('Yjs document contains unresolved delete dependencies')
    }
  }
}

const EDITOR_YJS_FORMAT_NAMES = new Set(
  Object.keys(EDITOR_YJS_SCHEMA.marks),
)

const EDITOR_YJS_NODE_NAMES = new Set(
  Object.keys(EDITOR_YJS_SCHEMA.nodes).filter((name) => name !== 'doc'),
)

const EDITOR_YJS_NODE_ATTRIBUTE_NAMES = new Set(
  Object.values(EDITOR_YJS_SCHEMA.nodes)
    .flatMap((node) => Object.keys(node.spec.attrs ?? {})),
)

function sameCanonicalYjsStructure(left: Y.Doc, right: Y.Doc): boolean {
  return sameCanonicalYjsNodes(
    left.getXmlFragment(EDITOR_YJS_FRAGMENT).toArray(),
    right.getXmlFragment(EDITOR_YJS_FRAGMENT).toArray(),
  )
}

function sameCanonicalYjsNodes(left: unknown[], right: unknown[]): boolean {
  if (left.length !== right.length) return false
  return left.every((node, index) => sameCanonicalYjsNode(node, right[index]))
}

function sameCanonicalYjsNode(left: unknown, right: unknown): boolean {
  if (left instanceof Y.XmlElement && right instanceof Y.XmlElement) {
    return left.nodeName === right.nodeName
      && sameCanonicalValue(attributeEntries(left), attributeEntries(right))
      && sameCanonicalYjsNodes(
        canonicalYjsElementChildren(left),
        canonicalYjsElementChildren(right),
      )
  }
  if (left instanceof Y.XmlText && right instanceof Y.XmlText) {
    return sameCanonicalValue(attributeEntries(left), attributeEntries(right))
      && sameCanonicalValue(left.toDelta(), right.toDelta())
  }
  if (
    left instanceof Y.XmlElement
    || left instanceof Y.XmlText
    || right instanceof Y.XmlElement
    || right instanceof Y.XmlText
  ) return false
  throw new Error('Yjs document contains a non-canonical XML child')
}

/**
 * A textblock without content holds one empty XmlText in the shared state its
 * writers create (see textblockPlaceholder.ts), and y-tiptap keeps that node
 * when the block's text is deleted. A block without any child is what y-tiptap
 * leaves when the block's last inline node is removed, and what documents
 * written without that text node hold. Both shapes read as the same empty
 * block, so only this exact, inert shape is normalized, on both sides of the
 * comparison. Additional children, text, marks/formatting or attributes
 * remain part of the strict structural comparison: two text nodes side by
 * side stay refused.
 */
function canonicalYjsElementChildren(element: Y.XmlElement): unknown[] {
  const children = element.toArray()
  if (
    EDITOR_YJS_TEXTBLOCK_NAMES.has(element.nodeName)
    && children.length === 1
    && isInertEmptyXmlText(children[0])
  ) {
    return []
  }
  return children
}

function isInertEmptyXmlText(node: unknown): node is Y.XmlText {
  return (
    node instanceof Y.XmlText
    && attributeEntries(node).length === 0
    && node.toDelta().length === 0
  )
}

/**
 * Every attribute of a type as sorted name and value pairs, read from its
 * attribute map itself. A plain object built from the attributes cannot hold
 * the key `__proto__`, so a comparison over one would let such an attribute
 * travel hidden in the document.
 */
function attributeEntries(type: Y.XmlElement | Y.XmlText): Array<[string, unknown]> {
  const entries: Array<[string, unknown]> = []
  for (const [name, item] of type._map) {
    if (item.deleted) continue
    entries.push([name, item.content.getContent()[item.length - 1]])
  }
  return entries.sort(([left], [right]) => left.localeCompare(right))
}

function sameCanonicalValue(left: unknown, right: unknown): boolean {
  if (left === null || right === null) return left === right
  if (typeof left === 'number' || typeof right === 'number') {
    if (
      typeof left !== 'number'
      || typeof right !== 'number'
      || !Number.isFinite(left)
      || !Number.isFinite(right)
    ) {
      throw new Error('Yjs document contains a non-finite value')
    }
    return left === right
  }
  if (
    typeof left === 'string'
    || typeof left === 'boolean'
    || typeof right === 'string'
    || typeof right === 'boolean'
  ) return left === right
  if (Array.isArray(left) || Array.isArray(right)) {
    if (!Array.isArray(left) || !Array.isArray(right) || left.length !== right.length) {
      return false
    }
    return left.every((value, index) => sameCanonicalValue(value, right[index]))
  }
  if (left && right && typeof left === 'object' && typeof right === 'object') {
    const leftPrototype = Object.getPrototypeOf(left)
    const rightPrototype = Object.getPrototypeOf(right)
    if (
      (leftPrototype !== Object.prototype && leftPrototype !== null)
      || (rightPrototype !== Object.prototype && rightPrototype !== null)
    ) {
      throw new Error('Yjs document contains a non-canonical attribute value')
    }
    const leftEntries = Object.entries(left).sort(([a], [b]) => a.localeCompare(b))
    const rightEntries = Object.entries(right).sort(([a], [b]) => a.localeCompare(b))
    return leftEntries.length === rightEntries.length
      && leftEntries.every(([key, value], index) => (
        key === rightEntries[index]?.[0]
        && sameCanonicalValue(value, rightEntries[index]?.[1])
      ))
  }
  if (left !== undefined || right !== undefined) return false
  throw new Error('Yjs document contains a non-canonical attribute value')
}
