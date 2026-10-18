import { mergeAttributes, Mark, type MarkConfig } from '@tiptap/core'

/**
 * The three physical ProseMirror marks used by the pinned suggest-changes
 * transformer. They are an implementation detail and deliberately differ
 * from the reader-facing change vocabulary below.
 */
export type SuggestionMarkKind = 'deletion' | 'insertion' | 'modification'

/** Reader-facing tracked-change vocabulary persisted in patch metadata. */
export type SuggestionKind =
  | 'deletion'
  | 'format'
  | 'insertion'
  | 'replacement'
  | 'structure'

/** `modification` is accepted when reading pre-v2 documents only. */
export type SerializedSuggestionKind = SuggestionKind | 'modification'

export type SuggestionMarkAttributes = {
  authorId: string | null
  createdAt: number | null
  id: string
  kind: SerializedSuggestionKind | null
  patchId: string | null
  suggestionId: string | null
}

const sharedSuggestionAttributes = {
  id: {
    default: null,
    validate: 'string',
  },
  suggestionId: {
    default: null,
    validate: 'string|null',
  },
  patchId: {
    default: null,
    validate: 'string|null',
  },
  authorId: {
    default: null,
    validate: 'string|null',
  },
  createdAt: {
    default: null,
    validate: 'number|null',
  },
  kind: {
    default: null,
    validate: 'string|null',
  },
}

function suggestionDataAttributes(attributes: Record<string, unknown>): Record<string, string> {
  return {
    'data-suggestion-id': String(attributes.suggestionId ?? attributes.id ?? ''),
    'data-suggestion-patch-id': String(attributes.patchId ?? ''),
    'data-suggestion-author-id': String(attributes.authorId ?? ''),
    'data-suggestion-created-at': String(attributes.createdAt ?? ''),
    'data-suggestion-kind': String(attributes.kind ?? ''),
  }
}

function parsedSuggestionAttributes(element: HTMLElement): Record<string, unknown> | false {
  const id = element.dataset.suggestionId
  if (!id) return false
  const createdAtValue = element.dataset.suggestionCreatedAt
  const createdAt = createdAtValue ? Number(createdAtValue) : null
  return {
    id,
    suggestionId: id,
    patchId: element.dataset.suggestionPatchId || null,
    authorId: element.dataset.suggestionAuthorId || null,
    createdAt: Number.isFinite(createdAt) ? createdAt : null,
    kind: element.dataset.suggestionKind || null,
  }
}

function suggestionMark(
  config: MarkConfig,
  extraAttributes: Record<string, { default?: unknown; validate?: string }> = {},
): Mark {
  return Mark.create({
    inclusive: false,
    ...config,
    addAttributes() {
      return { ...sharedSuggestionAttributes, ...extraAttributes }
    },
  })
}

export const SuggestionDeletion = suggestionMark({
  name: 'deletion',
  excludes: 'insertion modification deletion',
  parseHTML() {
    return [{ tag: 'del[data-suggestion-id]', getAttrs: parsedSuggestionAttributes }]
  },
  renderHTML({ HTMLAttributes, mark }) {
    return ['del', mergeAttributes(HTMLAttributes, suggestionDataAttributes(mark.attrs)), 0]
  },
})

export const SuggestionInsertion = suggestionMark({
  name: 'insertion',
  excludes: 'deletion modification insertion',
  parseHTML() {
    return [{ tag: 'ins[data-suggestion-id]', getAttrs: parsedSuggestionAttributes }]
  },
  renderHTML({ HTMLAttributes, mark }) {
    return ['ins', mergeAttributes(HTMLAttributes, suggestionDataAttributes(mark.attrs)), 0]
  },
})

export const SuggestionModification = suggestionMark({
  name: 'modification',
  excludes: 'deletion insertion',
  parseHTML() {
    return [{ tag: '[data-suggestion-type="modification"]', getAttrs: parsedSuggestionAttributes }]
  },
  renderHTML({ HTMLAttributes, mark }) {
    return [
      'span',
      mergeAttributes(HTMLAttributes, suggestionDataAttributes(mark.attrs), {
        'data-suggestion-type': 'modification',
      }),
      0,
    ]
  },
}, {
  type: { default: null, validate: 'string|null' },
  attrName: { default: null, validate: 'string|null' },
  previousValue: { default: null },
  newValue: { default: null },
})

export const SUGGESTION_MARK_NAMES: ReadonlySet<string> = new Set<SuggestionMarkKind>([
  'deletion',
  'insertion',
  'modification',
])
