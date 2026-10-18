/**
 * What a note IS.
 *
 * Choosing a type before writing a note is a decision nobody needs, and
 * filtering by it afterwards is a second one. There is exactly ONE authored
 * kind. What would otherwise be a type is an ACTION on the finished note
 * instead: collect it for the assistant, or run it right away.
 *
 * `assistant_edit` is not an authored note and never becomes one: it is the
 * line an assistant run attaches its private draft to, and only such a draft
 * authorises publishing that one suggestion (see `EditorCommentKind`).
 *
 * Existing rows are NOT migrated: a note written as `inline_edit` or
 * `evidence_review` is simply READ as the one kind, which is what
 * `isAuthoredComment` expresses. The column keeps its values and stops being
 * written with anything but `AUTHORED_COMMENT_KIND`.
 */
import type { EditorCommentKind } from '@/features/project/types'

/** The kind every new note is written with. */
export const AUTHORED_COMMENT_KIND: EditorCommentKind = 'collect'

/**
 * The single read mapping. Every legacy authored kind answers `true` here and
 * is treated identically from then on; only the machine carrier answers
 * `false`. Filtering on a literal kind anywhere else would be a second
 * definition and would silently drop old notes.
 */
export function isAuthoredComment(comment: { kind: EditorCommentKind }): boolean {
  return comment.kind !== 'assistant_edit'
}
