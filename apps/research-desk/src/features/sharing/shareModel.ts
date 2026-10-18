/** Pure sharing logic — kept free of React so vitest covers it directly. */

import type { ResearchRunAccess } from '@/features/researchRuns/types'
import type {
  OutgoingShare,
  SharePermissionValue,
  UserSearchResult,
} from './types'

const VIEW_EDIT_PERMISSIONS = ['view', 'edit'] as const
const EDITOR_DOCUMENT_PERMISSIONS = ['view', 'suggest', 'edit'] as const

export type SharedResourceDestination =
  | 'editor'
  | 'knowledge'
  | 'prompt-library'
  | 'research'

/** Grantable permissions mirror the server's resource-specific policy. */
export function sharePermissionsForResource(
  resourceType: string,
): readonly SharePermissionValue[] {
  return resourceType === 'editor_document'
    ? EDITOR_DOCUMENT_PERMISSIONS
    : VIEW_EDIT_PERMISSIONS
}

/** Exhaustive label selection shared by the dialog and inbox panel. */
export function sharePermissionLabel(
  permission: SharePermissionValue,
  locale: 'de' | 'en',
  labels: { edit: string; view: string },
): string {
  return permission === 'suggest'
    ? locale === 'de' ? 'Vorschlagen' : 'Suggest'
    : labels[permission]
}

/** Workspace destination for an accepted incoming share. */
export function sharedResourceDestination(
  resourceType: string,
): SharedResourceDestination {
  if (resourceType === 'editor_document') return 'editor'
  if (resourceType === 'knowledge_collection') return 'knowledge'
  if (resourceType === 'prompt_template' || resourceType === 'skill_template') {
    return 'prompt-library'
  }
  return 'research'
}

/**
 * Split a job list into own and shared-in entries while preserving
 * order. Shared-in jobs (the server's canonical `access.mode`)
 * render under the "Mit mir geteilt" divider, never mixed into the
 * caller's own runs.
 */
export function partitionJobsByAccess<T extends { access?: ResearchRunAccess }>(
  jobs: readonly T[],
): { own: T[]; shared: T[] } {
  const own: T[] = []
  const shared: T[] = []
  for (const job of jobs) {
    if (job.access?.mode === 'shared') shared.push(job)
    else own.push(job)
  }
  return { own, shared }
}

/**
 * Whether a shared-in run may be cancelled by the recipient. Mirrors
 * the server rule (cancel needs at least an edit grant) so the UI
 * never offers a button that would land as 404. Owner, unscoped, and local
 * runs keep the existing status-based gate.
 */
export function canCancelWithAccess(access: ResearchRunAccess | undefined): boolean {
  return access?.mode !== 'shared' || access.permission === 'edit'
}

/** Active recipient counts from the one `/v1/shares/mine` lifecycle list. */
export function outgoingShareCounts(
  entries: readonly OutgoingShare[],
  resourceType: string,
): Record<string, number> {
  return Object.fromEntries(
    entries
      .filter((entry) => entry.resource_type === resourceType)
      .map((entry) => [entry.resource_id, entry.share_count]),
  )
}

/** Toggle a user in the dialog's invitee selection (dedup by canonical id). */
export function toggleSelectedUser(
  selected: readonly UserSearchResult[],
  user: UserSearchResult,
): UserSearchResult[] {
  if (selected.some((entry) => entry.id === user.id)) {
    return selected.filter((entry) => entry.id !== user.id)
  }
  return [...selected, user]
}

/** Typeahead rows minus everyone who already holds a share or is picked. */
export function selectableSearchResults(
  results: readonly UserSearchResult[],
  existingUserIds: ReadonlySet<string>,
): UserSearchResult[] {
  return results.filter((user) => !existingUserIds.has(user.id))
}

/** Visible label for one person (display name first, email fallback). */
export function personLabel(
  displayName: string | null | undefined,
  email: string | null | undefined,
  fallback: string,
): string {
  return displayName?.trim() || email?.trim() || fallback
}
