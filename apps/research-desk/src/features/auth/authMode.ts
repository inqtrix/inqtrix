/**
 * Frontend auth-mode vocabulary, mirroring the server's `auth_mode`.
 *
 * Two derived predicates keep the `oidc || local` test from being
 * re-spelled across the component tree (shared logic is defined once):
 *  - cookie-session modes share the BFF session/CSRF machinery, so the SPA
 *    probes `/api/auth/session` and gates sharing/quota/admin on a live
 *    session for all three;
 *  - the password mode (`local`) renders the credential login form and
 *    drive the first-run owner setup gate.
 */
export type AuthMode = 'none' | 'apikey' | 'oidc' | 'local'

const KNOWN_MODES: readonly string[] = ['none', 'apikey', 'oidc', 'local']

/** Whether the server reported a mode this build knows how to render.
 *
 * The type is erased at runtime, so a server ahead of (or behind) this
 * bundle can report anything. An unrecognised mode falls through to the
 * token form, which cannot unlock it -- so the fallthrough is announced
 * rather than silent (no silent fallbacks).
 */
export function isKnownAuthMode(mode: string | undefined | null): boolean {
  return typeof mode === 'string' && KNOWN_MODES.includes(mode)
}

/** Modes whose identity rides a server-side cookie session. */
export function isCookieSessionMode(mode: AuthMode | undefined | null): boolean {
  return mode === 'oidc' || mode === 'local'
}

/** Modes that authenticate via an identifier + password form. */
export function isPasswordMode(mode: AuthMode | undefined | null): boolean {
  return mode === 'local'
}
