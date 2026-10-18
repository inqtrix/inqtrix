import { createHash } from 'node:crypto'

export const VERIFICATION_RUN_ID_PATTERN =
  /^inqv-[a-z0-9][a-z0-9-]{7,75}$/

export function assertVerificationRunId(value, label = 'verification Run ID') {
  if (
    typeof value !== 'string'
    || !VERIFICATION_RUN_ID_PATTERN.test(value)
  ) {
    throw new Error(`${label} is invalid.`)
  }
  return value
}

export function documentBelongsToRun(documentId, runId) {
  assertVerificationRunId(runId)
  return typeof documentId === 'string'
    && documentId.startsWith(`ed_${runId}_`)
}

export function agentSessionTitleForRun(runId) {
  // Agent sessions adopt the submitted question as their title (the FE
  // sync derives 80 chars, the server-side claim keeps 120), so a
  // run-PREFIXED question is the one available Run-ID binding for
  // server-generated session/run ids — binding is startsWith, never
  // equality, so both truncations qualify. Generated verification run
  // ids (~28 chars) leave ample headroom before either cut.
  assertVerificationRunId(runId)
  return `${runId} Agent-Desk-Verifikation`
}

export function agentSessionBelongsToRun(title, runId) {
  assertVerificationRunId(runId)
  return typeof title === 'string' && title.startsWith(`${runId} `)
}

const MAX_TEMPORARY_USERS_PER_RUN = 24

export function temporaryUserDescriptors(runId, count = 4) {
  assertVerificationRunId(runId)
  if (!Number.isSafeInteger(count) || count < 1 || count > MAX_TEMPORARY_USERS_PER_RUN) {
    throw new Error(
      `Temporary user provisioning requires between 1 and ${MAX_TEMPORARY_USERS_PER_RUN} users; at most 24 are allowed.`,
    )
  }
  const fragment = runId
    .replace(/^inqv-/, '')
    .replaceAll(/[^a-z0-9-]+/g, '-')
    .slice(0, 28)
  const digest = createHash('sha256').update(runId).digest('hex').slice(0, 10)
  return Array.from({ length: count }, (_, index) => index + 3).map((number) => ({
    displayName: `Inqv ${fragment} Nutzer ${number}`.slice(0, 80),
    email: `inqv-${fragment}-${digest}-u${number}@example.invalid`,
  }))
}

export function temporaryUserBelongsToRun(email, runId) {
  return temporaryUserDescriptors(runId, MAX_TEMPORARY_USERS_PER_RUN).some(
    (descriptor) => descriptor.email === email,
  )
}
