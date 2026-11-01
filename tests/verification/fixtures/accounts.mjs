import { setTimeout as delay } from 'node:timers/promises'

import { assertFixture, fetchActorJson } from './api.mjs'
import { temporaryUserDescriptors } from './run-scope.mjs'

export {
  temporaryUserBelongsToRun,
  temporaryUserDescriptors,
} from './run-scope.mjs'

export async function ensureTemporaryUsers({
  adminActor,
  lifecycle,
  password,
  runId,
  descriptors = temporaryUserDescriptors(runId),
}) {
  const listed = await fetchActorJson(adminActor, 'GET', '/v1/admin/users')
  const rows = []
  for (const descriptor of descriptors) {
    const cleanupHandle = await lifecycle.register({
      email: descriptor.email,
      id: `${runId}:${descriptor.email}`,
      kind: 'temporary_user',
    })
    let user = listed.users.find(
      (candidate) => candidate.email === descriptor.email,
    )
    if (!user) {
      user = await fetchActorJson(adminActor, 'POST', '/v1/admin/users', {
        data: {
          display_name: descriptor.displayName,
          email: descriptor.email,
          instance_role: 'user',
          password,
        },
        expected: [201],
      })
    } else {
      await fetchActorJson(
        adminActor,
        'POST',
        `/v1/admin/users/${user.id}:enable`,
      )
      await fetchActorJson(
        adminActor,
        'POST',
        `/v1/admin/users/${user.id}:reset-password`,
        { data: { password } },
      )
    }
    assertFixture(user?.id, 'Temporary user creation returned no user ID.')
    rows.push({ ...descriptor, cleanupHandle, id: user.id })
  }
  return rows
}

/**
 * Save the interface language of an account this run created.
 *
 * An account without a saved language shows the product default, while the
 * run's pages look for their labels in the run's language. The save replaces
 * the whole preference row (theme, contrast, agent memory and model tiers
 * included), so only accounts the run created are written; the row goes
 * with the account when the run erases it.
 */
export async function setAccountLocale(actor, locale, { now = Date.now } = {}) {
  await fetchActorJson(actor, 'PUT', '/v1/account/preferences', {
    data: { locale, updated_at: Math.floor(now() / 1000) },
  })
  const saved = await savedAccountLocale(actor)
  assertFixture(
    saved === locale,
    `${actor.label} kept ${describeLocale(saved)} after saving '${locale}'.`,
  )
}

/**
 * Stop the run when an account has another saved interface language than
 * the run's.
 *
 * A borrowed account, such as the operator's admin, is only read: writing
 * would replace its other preferences as well.
 */
export async function requireAccountLocale(actor, locale) {
  const saved = await savedAccountLocale(actor)
  assertFixture(
    saved === locale,
    `${actor.label} has ${describeLocale(saved)}; the run expects '${locale}'. `
      + "Set it in that account's settings.",
  )
}

async function savedAccountLocale(actor) {
  const saved = await fetchActorJson(actor, 'GET', '/v1/account/preferences', {
    expected: [200, 404],
  })
  return typeof saved?.locale === 'string' ? saved.locale : null
}

function describeLocale(saved) {
  return saved === null
    ? 'no saved interface language'
    : `the interface language '${saved}'`
}

/** An erasure waits for a deletion worker; a short wait would report a
 * temporary account as cleaned while it still exists. */
const ERASURE_TIMEOUT_MS = 120_000
const ERASURE_POLL_MS = 500

/**
 * Erase a temporary account and complete its cleanup entry once the account
 * is gone. Disabling it instead left every run's accounts and their
 * documents behind.
 */
export async function eraseTemporaryUser(
  adminActor,
  user,
  lifecycle,
  { now = Date.now, sleep = delay } = {},
) {
  const operation = await fetchActorJson(
    adminActor,
    'DELETE',
    `/v1/admin/users/${user.id}`,
    { expected: [202, 404] },
  )
  if (operation?.operation_id) {
    const deadline = now() + ERASURE_TIMEOUT_MS
    for (;;) {
      // An account erasure belongs to no workspace: read it without one,
      // or the answer is a 404 for an erasure that still runs.
      const current = await fetchActorJson(
        adminActor,
        'GET',
        `/v1/deletion-operations/${encodeURIComponent(operation.operation_id)}`,
        { workspace: false },
      )
      if (current.status === 'deleted') break
      if (current.status === 'delete_failed') {
        throw new Error(
          `Temporary account erasure failed (${current.error?.type ?? 'unknown'}).`,
        )
      }
      if (now() >= deadline) {
        throw new Error(
          `Temporary account erasure did not finish within ${ERASURE_TIMEOUT_MS} ms.`,
        )
      }
      await sleep(ERASURE_POLL_MS)
    }
  }
  await lifecycle.complete(user.cleanupHandle)
}
