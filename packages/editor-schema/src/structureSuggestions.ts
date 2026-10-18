import { Extension } from '@tiptap/core'

import type { SuggestionKind } from './suggestionMarks.js'

export const INQTRIX_STRUCTURE_COMMAND_META = 'inqtrixStructureCommand'
export const INQTRIX_STRUCTURE_SUGGESTION_ATTR = 'inqtrixStructureSuggestion'

export type StructureSuggestionAction =
  | 'blockquote'
  | 'bulletList'
  | 'codeBlock'
  | 'heading1'
  | 'heading2'
  | 'heading3'
  | 'orderedList'
  | 'paragraph'
  | 'taskList'

export type StructureSuggestionCommand = {
  action: StructureSuggestionAction | 'divider' | 'table'
  commandRange?: {
    from: number
    to: number
  }
}

export type StructureSuggestionData = {
  action: StructureSuggestionAction
  authorId: string
  createdAt: number
  discardedCommand?: {
    offset: number
    text: string
  }
  kind: Extract<SuggestionKind, 'structure'>
  patchId: string
  suggestionId: string
}

/**
 * Structural proposals live on the original text block instead of replacing
 * it. That keeps the Yjs topology stable until a reviewer decides, supports
 * empty slash-command blocks, and lets accept/reject preserve concurrent text.
 * The metadata is intentionally omitted from rendered HTML/Markdown.
 */
export const StructureSuggestionAttributes = Extension.create({
  name: 'inqtrixStructureSuggestionAttributes',

  addGlobalAttributes() {
    return [{
      types: ['codeBlock', 'heading', 'paragraph'],
      attributes: {
        [INQTRIX_STRUCTURE_SUGGESTION_ATTR]: {
          default: null,
          // A pending structural suggestion belongs to the one block it was
          // made on. A block split off it by Enter starts without it;
          // otherwise both blocks would carry the same suggestion id.
          keepOnSplit: false,
          parseHTML: () => null,
          renderHTML: () => ({}),
        },
      },
    }]
  },
})

export function isStructureSuggestionData(value: unknown): value is StructureSuggestionData {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false
  const data = value as Record<string, unknown>
  if (
    !STRUCTURE_ACTIONS.has(data.action as StructureSuggestionAction)
    || typeof data.authorId !== 'string'
    || data.authorId.length === 0
    || !Number.isSafeInteger(data.createdAt)
    || Number(data.createdAt) < 0
    || data.kind !== 'structure'
    || typeof data.patchId !== 'string'
    || data.patchId.length === 0
    || typeof data.suggestionId !== 'string'
    || data.suggestionId.length === 0
  ) return false
  const discarded = data.discardedCommand
  return discarded === undefined || (
    discarded !== null
    && typeof discarded === 'object'
    && !Array.isArray(discarded)
    && Number.isSafeInteger(Reflect.get(discarded, 'offset'))
    && Number(Reflect.get(discarded, 'offset')) >= 0
    && typeof Reflect.get(discarded, 'text') === 'string'
    && String(Reflect.get(discarded, 'text')).length <= 96
  )
}

export const STRUCTURE_ACTIONS: ReadonlySet<StructureSuggestionAction> = new Set([
  'blockquote',
  'bulletList',
  'codeBlock',
  'heading1',
  'heading2',
  'heading3',
  'orderedList',
  'paragraph',
  'taskList',
])
