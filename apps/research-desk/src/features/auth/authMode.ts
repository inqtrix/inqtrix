/**
 * Frontend auth-mode vocabulary, mirroring the server's `auth_mode`.
 *
 * Derived predicates keep the `oidc || local || gateway` test from being
 * re-spelled across the component tree (shared logic is defined once):
 *  - cookie-session modes share the BFF session/CSRF machinery, so the SPA
 *    probes `/api/auth/session` and gates sharing/quota/admin on a live
 *    session for all three;
 *  - the password mode (`local`) renders the credential login form and
 *    drive the first-run owner setup gate;
 *  - the gateway mode renders no form at all: a gateway in front of
 *    Inqtrix signs the user in, and the sign-in surface shows the state of
 *    the session start instead.
 */
export type AuthMode = 'none' | 'apikey' | 'oidc' | 'local' | 'gateway'

const KNOWN_MODES: readonly string[] = ['none', 'apikey', 'oidc', 'local', 'gateway']

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
  return mode === 'oidc' || mode === 'local' || mode === 'gateway'
}

/** Modes that authenticate via an identifier + password form. */
export function isPasswordMode(mode: AuthMode | undefined | null): boolean {
  return mode === 'local'
}

/** The mode the desk runs in: the one the server reports in its health
 * answer. Without that answer the desk runs open (`none`), except in a
 * browser that stored a gateway sign-out: the gateway then hides the server
 * because the person signed out there, so the desk stays in gateway mode and
 * shows the signed-out page instead of an open desk. */
export function deskAuthMode(
  health: { auth_mode?: AuthMode; auth_required?: boolean } | null | undefined,
  { demo, gatewaySignedOut }: { demo: boolean; gatewaySignedOut: boolean },
): AuthMode {
  if (demo) return 'none'
  if (health) return health.auth_mode ?? (health.auth_required ? 'apikey' : 'none')
  return gatewaySignedOut ? 'gateway' : 'none'
}

/** The mode in which a gateway in front of Inqtrix signs the user in. */
export function isGatewayMode(mode: AuthMode | undefined | null): boolean {
  return mode === 'gateway'
}

/**
 * The one state the sign-in surface shows in gateway mode for an answer of
 * the session start, including an answer that did not come from Inqtrix.
 */
export type GatewayState =
  | 'noGatewaySignIn'
  | 'renewGatewaySignIn'
  | 'signedOut'
  | 'signedOutAtGateway'
  | 'accountDisabled'
  | 'notProvisioned'
  | 'accessNotGranted'
  | 'emailUnverified'
  | 'signInFailed'
  | 'serviceUnavailable'

/** A state and the outcome class behind it. The class carries no identity
 * value, so the page may show it as an identifier for the administration. */
export type GatewayStateInfo = {
  state: GatewayState
  outcome: string | null
}

/** What the session start answered, as far as the state depends on it. */
export type GatewayAnswer =
  | { kind: 'unauthenticated'; reason: string | null }
  | { kind: 'rejected'; outcome: string }
  | { kind: 'foreign' }
  | { kind: 'signedOut'; atGateway: boolean }

/** What a state offers: a full reload, a new probe, a new sign-in on click,
 * or only a hint (`null`). */
export type GatewayAction = 'reload' | 'retry' | 'signInAgain' | null

const GATEWAY_STATE_OF_OUTCOME: ReadonlyMap<string, GatewayState> = new Map([
  ['assertion_missing', 'noGatewaySignIn'],
  ['account_disabled', 'accountDisabled'],
  ['not_provisioned', 'notProvisioned'],
  ['registration_denied', 'accessNotGranted'],
  ['admission_denied', 'accessNotGranted'],
  ['email_unverified', 'emailUnverified'],
  ['keys_unavailable', 'serviceUnavailable'],
  ['resolver_failed', 'serviceUnavailable'],
])

export const GATEWAY_ACTIONS: Readonly<Record<GatewayState, GatewayAction>> = {
  noGatewaySignIn: 'reload',
  renewGatewaySignIn: 'reload',
  signedOut: 'signInAgain',
  signedOutAtGateway: 'signInAgain',
  accountDisabled: null,
  notProvisioned: null,
  accessNotGranted: null,
  emailUnverified: null,
  signInFailed: 'reload',
  serviceUnavailable: 'retry',
}

/** How the sign-in surface announces a state: a failure or a refusal at
 * once (`alert`), a state with a plain next step politely (`status`). */
export const GATEWAY_STATE_ROLES: Readonly<Record<GatewayState, 'alert' | 'status'>> = {
  noGatewaySignIn: 'status',
  renewGatewaySignIn: 'status',
  signedOut: 'status',
  signedOutAtGateway: 'status',
  accountDisabled: 'alert',
  notProvisioned: 'alert',
  accessNotGranted: 'alert',
  emailUnverified: 'alert',
  signInFailed: 'alert',
  serviceUnavailable: 'alert',
}

/** The state for an answer of the session start. Every class without a state
 * of its own, assertion failures included, reads as a failed gateway sign-in:
 * never the token form, which cannot unlock this mode. */
export function gatewayState(answer: GatewayAnswer): GatewayStateInfo {
  if (answer.kind === 'foreign') return { state: 'renewGatewaySignIn', outcome: null }
  if (answer.kind === 'signedOut') {
    return { state: answer.atGateway ? 'signedOutAtGateway' : 'signedOut', outcome: null }
  }
  const outcome = answer.kind === 'unauthenticated' ? answer.reason : answer.outcome
  const state = outcome === null ? undefined : GATEWAY_STATE_OF_OUTCOME.get(outcome)
  return { state: state ?? 'signInFailed', outcome }
}

/** The answer behind a session payload, or `null` for a signed-in user. A
 * payload without the session shape came from something in front of Inqtrix. */
export function gatewaySessionAnswer(payload: unknown): GatewayAnswer | null {
  if (
    typeof payload !== 'object'
    || payload === null
    || typeof (payload as { authenticated?: unknown }).authenticated !== 'boolean'
  ) {
    return { kind: 'foreign' }
  }
  if ((payload as { authenticated: boolean }).authenticated) return null
  const reason = (payload as { reason?: unknown }).reason
  return { kind: 'unauthenticated', reason: typeof reason === 'string' ? reason : null }
}

/** The form of every class Inqtrix names; a type in any other form comes
 * from something in front of Inqtrix that answers in its own error format. */
const OUTCOME_CLASS = /^[a-z][a-z0-9_]{0,63}$/

/** The answer behind a failed session start. A typed Inqtrix error names its
 * class; a network error, a status without a typed body, a body that is no
 * JSON, or a type that is not in the form of an Inqtrix class came from an
 * API gateway's own login or error page. */
export function gatewayAnswerFromError(error: unknown): GatewayAnswer {
  if (
    error instanceof Error
    && typeof (error as { status?: unknown }).status === 'number'
    && OUTCOME_CLASS.test(error.name)
  ) {
    return { kind: 'rejected', outcome: error.name }
  }
  return { kind: 'foreign' }
}

/** How long after an automatic reload the page holds the next one back. */
const AUTOMATIC_RELOAD_PAUSE_MS = 30_000

/**
 * What the page does about a 401 that took the identity it shows. The
 * gateway starts a session on every load, so a session that never reaches
 * the next request (a cookie the browser drops, an assertion only on the
 * session start) would reload the page without end: within the pause after
 * an automatic reload the page holds the next one back and shows the failed
 * sign-in instead. A reload this page life started is underway, so a second
 * 401 changes nothing. `lastReloadAt` is `null` when no reload is known.
 */
export function automaticReloadStep({
  lastReloadAt,
  now,
  reloadStarted,
}: {
  lastReloadAt: number | null
  now: number
  reloadStarted: boolean
}): 'reload' | 'hold' | 'underway' {
  if (reloadStarted) return 'underway'
  if (lastReloadAt === null) return 'reload'
  const elapsed = now - lastReloadAt
  // A time ahead of the clock means the clock moved back; holding until it
  // catches up could take any time.
  return elapsed >= 0 && elapsed < AUTOMATIC_RELOAD_PAUSE_MS ? 'hold' : 'reload'
}

/** The state shown for a held reload: the session start succeeded and the
 * request after it failed, so the gateway sign-in failed, with the class of
 * the 401 when Inqtrix named one. */
export function heldReloadState(error: unknown): GatewayStateInfo {
  const answer = gatewayAnswerFromError(error)
  return { state: 'signInFailed', outcome: answer.kind === 'rejected' ? answer.outcome : null }
}
