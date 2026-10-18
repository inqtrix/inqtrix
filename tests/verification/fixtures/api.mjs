import { requestFailureKind } from './request-failure.mjs'

export function assertFixture(condition, message) {
  if (!condition) throw new Error(message)
}

/** Literal browser cancellation codes; console and HTTP errors stay failures. */
export function isExpectedBrowserRequestAbort(failure) {
  return /^(?:request:.*:)?(?:net::ERR_ABORTED|ERR_ABORTED|NS_BINDING_ABORTED|cancelled|Load request cancelled)$/.test(failure)
}

export async function fetchActorJson(actor, method, path, options = {}) {
  const expected = options.expected ?? [200]
  const unsafe = !['GET', 'HEAD'].includes(method)
  const headers = {
    'X-Inqtrix-Expected-User-Id': actor.user.id,
  }
  // `workspace: false` for a resource that belongs to no workspace, such as
  // an account erasure: a request that names one does not find it.
  if (options.workspace !== false) {
    headers['X-Inqtrix-Workspace-Id'] = actor.workspaceId
  }
  if (unsafe) headers['X-CSRF-Token'] = actor.csrf
  const { request } = actor.context
  let response
  try {
    // Only the request itself is wrapped: its errors are the ones that
    // carry the request's headers.
    response = await request.fetch(path, {
      data: options.data,
      headers,
      method,
      multipart: options.multipart,
    })
  } catch (error) {
    throw new Error(
      `${actor.label} ${method} ${redactPath(path)} ${requestFailureKind(error)}`,
    )
  }
  const text = await response.text()
  let body = null
  if (text) {
    try {
      body = JSON.parse(text)
    } catch {
      body = { text: text.slice(0, 500) }
    }
  }
  if (!expected.includes(response.status())) {
    throw new Error(
      `${actor.label} ${method} ${redactPath(path)} returned HTTP `
      + `${response.status()}: ${JSON.stringify(body)}`,
    )
  }
  return body
}

export function redactPath(path) {
  return String(path)
    .replace(/\/s\/[^/?#\s]+/gi, '/s/[REDACTED]')
    .replace(
      /\/v1\/editor\/share-links\/[^/?#\s:]+/gi,
      '/v1/editor/share-links/[REDACTED]',
    )
}
