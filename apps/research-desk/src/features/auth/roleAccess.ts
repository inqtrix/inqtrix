/**
 * Instance-role gate for the admin surface.
 *
 * The instance role (`admin`/`user`) is distinct from a workspace role
 * (viewer/commenter/editor/owner): it is instance-wide and drives whether
 * the admin section exists at all. The check is deliberately default-CLOSED
 * — anything that is not exactly the admin string (a missing role on an
 * older backend, `null` while the session is still unknown, an unexpected
 * value) yields `false`, so the admin UI is never even constructed unless
 * the server affirmatively says so. Mirrors the backend, where
 * `session_payload` resolves `role` from the mirror and falls back to
 * `user` fail-closed.
 */

/** The single instance role that unlocks the admin surface. */
export const ADMIN_ROLE = 'admin'

/** Whether *role* grants the instance-admin surface (default-closed). */
export function isAdminRole(role: string | null | undefined): boolean {
  return role === ADMIN_ROLE
}
