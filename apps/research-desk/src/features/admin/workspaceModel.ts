import type { AdminUser, WorkspaceMember } from '@/api/inqtrixClient'
import { isErasing } from './adminModel'

/**
 * Whether removing or demoting *userId* would strip the workspace's only OWNER.
 *
 * The server enforces this (409 ``last_owner``); the same pure rule lives here
 * so the UI can pre-empt it (disable the control) and the demo twin can show
 * the guard offline. *keepsOwner* is ``true`` when the operation leaves *sub*
 * an owner (a role change that stays ``owner``), which is never an orphan.
 */
export function wouldOrphanLastOwner(
  members: ReadonlyArray<WorkspaceMember>,
  userId: string,
  keepsOwner: boolean,
): boolean {
  if (keepsOwner) return false
  const owners = members.filter((member) => member.role === 'owner')
  return owners.length === 1 && owners[0]?.user_id === userId
}

/**
 * The add-member typeahead candidate pool: users not yet in the workspace that
 * match *query* (a case-insensitive substring of display name, email, or id),
 * name-sorted. An empty query returns the full (member/disabled-filtered) list.
 *
 * Source is the loaded ADMIN user list, deliberately NOT ``/v1/users/search``:
 * that endpoint is narrowed to the caller's co-members when workspace-scoped
 * sharing is on, which is wrong for workspace ADMINISTRATION (an admin must be
 * able to position any tenant user). Disabled users are excluded — the server's
 * canonical-user gate on the assign endpoint rejects them (404) — keeping the
 * picker truthful (and the demo twin faithful to the backend). Accounts being
 * erased are excluded as well: they are cut off already, and every membership
 * they hold goes with them.
 */
export function candidateUsers(
  users: ReadonlyArray<AdminUser>,
  memberUserIds: ReadonlySet<string>,
  query = '',
): AdminUser[] {
  const needle = query.trim().toLowerCase()
  return users
    .filter(
      (user) =>
        !user.disabled && !isErasing(user) && !memberUserIds.has(user.id),
    )
    .filter(
      (user) =>
        needle === '' ||
        (user.display_name ?? '').toLowerCase().includes(needle) ||
        (user.email ?? '').toLowerCase().includes(needle) ||
        user.id.toLowerCase().includes(needle),
    )
    .sort((a, b) =>
      (a.display_name ?? a.email ?? a.id).localeCompare(
        b.display_name ?? b.email ?? b.id,
      ),
    )
}
