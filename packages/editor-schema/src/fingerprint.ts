import { getSchema } from '@tiptap/core'
import type { AttributeSpec, MarkSpec, NodeSpec } from '@tiptap/pm/model'
import {
  EDITOR_COLLABORATION_PROTOCOL_VERSION,
  EDITOR_SCHEMA_BEHAVIOR_INPUTS,
  EDITOR_SCHEMA_DEPENDENCY_VERSIONS,
  EDITOR_SCHEMA_VERSION,
  EDITOR_YJS_FRAGMENT,
} from './constants.js'
import { createEditorSchemaExtensions } from './extensions.js'

let schemaFingerprint: Promise<string> | null = null

export type EditorSchemaAttributeDescriptor = {
  default: unknown
  name: string
  order: number
  validate: string | null
}

export type EditorSchemaItemDescriptor = {
  name: string
  order: number
  rank: number | null
  spec: Record<string, unknown>
}

export type EditorSchemaFingerprintDescriptor = {
  behavior: typeof EDITOR_SCHEMA_BEHAVIOR_INPUTS
  dependencies: typeof EDITOR_SCHEMA_DEPENDENCY_VERSIONS
  extensions: Array<{ name: string; order: number }>
  fragment: string
  marks: EditorSchemaItemDescriptor[]
  nodes: EditorSchemaItemDescriptor[]
  protocolVersion: number
  schemaVersion: number
  topNode: string
}

export function getEditorSchemaFingerprint(): Promise<string> {
  schemaFingerprint ??= fingerprintEditorSchemaDescriptor(editorSchemaDescriptor())
  return schemaFingerprint
}

export function fingerprintEditorSchemaDescriptor(descriptor: unknown): Promise<string> {
  return sha256Hex(stableJson(descriptor))
}

export function editorSchemaDescriptor(): EditorSchemaFingerprintDescriptor {
  const extensions = createEditorSchemaExtensions({ enableUndoRedo: false })
  const schema = getSchema(extensions)
  const nodes: EditorSchemaItemDescriptor[] = []
  const marks: EditorSchemaItemDescriptor[] = []
  let nodeOrder = 0
  schema.spec.nodes.forEach((name, spec) => {
    nodes.push({
      name,
      order: nodeOrder,
      rank: null,
      spec: schemaItemDescriptor(spec),
    })
    nodeOrder += 1
  })
  let markOrder = 0
  schema.spec.marks.forEach((name, spec) => {
    marks.push({
      name,
      order: markOrder,
      rank: markOrder,
      spec: schemaItemDescriptor(spec),
    })
    markOrder += 1
  })
  return {
    behavior: EDITOR_SCHEMA_BEHAVIOR_INPUTS,
    dependencies: EDITOR_SCHEMA_DEPENDENCY_VERSIONS,
    extensions: extensions.map((extension, order) => ({ name: extension.name, order })),
    fragment: EDITOR_YJS_FRAGMENT,
    marks,
    nodes,
    protocolVersion: EDITOR_COLLABORATION_PROTOCOL_VERSION,
    schemaVersion: EDITOR_SCHEMA_VERSION,
    topNode: schema.topNodeType.name,
  }
}

function schemaItemDescriptor(spec: NodeSpec | MarkSpec): Record<string, unknown> {
  const attributes = Object.entries(spec.attrs ?? {}).map(([name, attribute], order) => (
    attributeDescriptor(name, attribute, order)
  ))
  return {
    atom: 'atom' in spec ? spec.atom ?? false : null,
    attrs: attributes,
    code: spec.code ?? false,
    content: 'content' in spec ? spec.content ?? null : null,
    defining: 'defining' in spec ? spec.defining ?? false : false,
    definingAsContext: 'definingAsContext' in spec ? spec.definingAsContext ?? false : null,
    definingForContent: 'definingForContent' in spec ? spec.definingForContent ?? false : null,
    draggable: 'draggable' in spec ? spec.draggable ?? false : null,
    excludes: 'excludes' in spec ? spec.excludes ?? null : null,
    group: spec.group ?? null,
    inclusive: 'inclusive' in spec ? spec.inclusive ?? true : null,
    inline: 'inline' in spec ? spec.inline ?? false : null,
    isolating: 'isolating' in spec ? spec.isolating ?? false : false,
    linebreakReplacement: 'linebreakReplacement' in spec
      ? spec.linebreakReplacement ?? false
      : null,
    marks: 'marks' in spec ? spec.marks ?? null : null,
    parseRuleCount: spec.parseDOM?.length ?? 0,
    selectable: 'selectable' in spec ? spec.selectable ?? true : null,
    spanning: 'spanning' in spec ? spec.spanning ?? true : null,
    toDOM: spec.toDOM ? '__versioned_function__' : null,
    whitespace: 'whitespace' in spec ? spec.whitespace ?? 'normal' : null,
  }
}

function attributeDescriptor(
  name: string,
  attribute: AttributeSpec,
  order: number,
): EditorSchemaAttributeDescriptor {
  return {
    default: Object.prototype.hasOwnProperty.call(attribute, 'default')
      ? stableValue(attribute.default)
      : '__required__',
    name,
    order,
    validate: typeof attribute.validate === 'string'
      ? attribute.validate
      : attribute.validate
        ? '__function__'
        : null,
  }
}

function stableValue(value: unknown): unknown {
  if (value === undefined) return '__undefined__'
  if (typeof value === 'function') return '__function__'
  return value
}

function stableJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stableJson).join(',')}]`
  if (value && typeof value === 'object') {
    return `{${Object.entries(value)
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([key, item]) => `${JSON.stringify(key)}:${stableJson(item)}`)
      .join(',')}}`
  }
  return JSON.stringify(value)
}

async function sha256Hex(value: string): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(value))
  return [...new Uint8Array(digest)]
    .map((byte) => byte.toString(16).padStart(2, '0'))
    .join('')
}
