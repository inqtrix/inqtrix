import { randomUUID } from 'node:crypto'

import { assertFixture, fetchActorJson, redactPath } from './api.mjs'

export async function grantAndAccept({
  lifecycle,
  owner,
  document,
  recipients,
}) {
  const response = await fetchActorJson(owner, 'POST', '/v1/shares', {
    data: {
      invitees: recipients.map(([actor, permission]) => ({
        permission,
        user_id: actor.user.id,
      })),
      resource_id: document.id,
      resource_type: 'editor_document',
    },
    expected: [201],
  })
  for (const [actor] of recipients) {
    const share = response.data.find(
      (candidate) => candidate.recipient_user_id === actor.user.id,
    )
    assertFixture(
      share,
      `Missing share row for ${actor.label} on ${document.title}.`,
    )
    await lifecycle.register({
      credential: owner.credential,
      documentId: document.id,
      id: share.id,
      kind: 'share',
      ownerEmail: owner.email,
    })
    await fetchActorJson(actor, 'POST', `/v1/shares/${share.id}/accept`)
  }
}

export async function createGuestLink({
  lifecycle,
  owner,
  document,
  permission,
}) {
  const response = await fetchActorJson(
    owner,
    'POST',
    `/v1/editor/documents/${document.id}/share-links`,
    {
      data: {
        command_id: randomUUID(),
        generation: document.generation,
        permission,
        ttl_seconds: 3600,
      },
      expected: [201],
    },
  )
  assertFixture(
    response?.data?.permission === permission
      && typeof response.data.url === 'string'
      && typeof response.data.password === 'string',
    `Guest link creation failed for permission ${permission}.`,
  )
  const cleanupHandle = await lifecycle.register({
    credential: owner.credential,
    documentId: document.id,
    id: response.data.id,
    kind: 'guest_link',
    ownerEmail: owner.email,
  })
  return { ...response.data, cleanupHandle }
}

export async function updateGuestLink({
  owner,
  document,
  link,
  permission,
}) {
  const response = await fetchActorJson(
    owner,
    'PATCH',
    `/v1/editor/documents/${document.id}/share-links/${link.id}`,
    {
      data: {
        command_id: randomUUID(),
        expected_revision: link.revision,
        permission,
      },
    },
  )
  return {
    ...response.data,
    cleanupHandle: link.cleanupHandle,
    password: link.password,
    url: link.url,
  }
}

export async function rotateGuestPassword({ owner, document, link }) {
  const response = await fetchActorJson(
    owner,
    'POST',
    `/v1/editor/documents/${document.id}/share-links/${link.id}:rotate-password`,
    {
      data: {
        command_id: randomUUID(),
        expected_revision: link.revision,
      },
    },
  )
  return {
    ...response.data,
    cleanupHandle: link.cleanupHandle,
    url: link.url,
  }
}

export async function revokeGuestLink({
  lifecycle,
  owner,
  document,
  link,
}) {
  const response = await fetchActorJson(
    owner,
    'DELETE',
    `/v1/editor/documents/${document.id}/share-links/${link.id}`,
    {
      data: {
        command_id: randomUUID(),
        expected_revision: link.revision,
      },
    },
  )
  await lifecycle.complete(link.cleanupHandle)
  return {
    ...response.data,
    cleanupHandle: link.cleanupHandle,
    password: link.password,
    url: link.url,
  }
}

export function guestDescribePath(link) {
  const token = new URL(link.url).pathname.split('/').at(-1)
  return `/v1/editor/share-links/${encodeURIComponent(token)}`
}

export function guestUnlockPath(link) {
  return `${guestDescribePath(link)}:unlock`
}

export { redactPath as redactGuestPath }
