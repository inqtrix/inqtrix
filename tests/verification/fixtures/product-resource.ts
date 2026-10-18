import type { ChildProcess } from 'node:child_process'
import { unlink } from 'node:fs/promises'
import { isAbsolute, relative, resolve } from 'node:path'

import {
  request,
  type APIRequestContext,
  type APIResponse,
} from '@playwright/test'

import type { CleanupHandle, CleanupLedger } from '../cleanup-ledger.ts'
import type { CleanupDisposition } from '../model.ts'
import type { RunContext } from '../run-context.ts'
import {
  agentSessionBelongsToRun,
  documentBelongsToRun,
  temporaryUserBelongsToRun,
} from './run-scope.mjs'
import {
  cleanupOwnedProjectDocuments,
} from './project-documents.mjs'
import { resolveFaultControlContainers } from './fault-control-server.mjs'
import { cleanupVerificationNetworkShape } from './network-shaping.mjs'
import { requestFailureKind } from './request-failure.mjs'

const PROTOCOL = 'inqtrix-verification-resource-v1'
const DELETION_TERMINAL_TIMEOUT_MS = 10_000
/** An account erasure waits for a deletion worker and removes every
 * aggregate the account owns; it gets longer than a single resource. */
const ACCOUNT_ERASURE_TIMEOUT_MS = 120_000

type CredentialKind = 'admin' | 'user'

export type ProductResource = (
  | {
      credential: CredentialKind
      documentId: string
      id: string
      kind: 'guest_link' | 'share'
      ownerEmail: string
    }
  | {
      credential: CredentialKind
      id: string
      kind: 'document'
      ownerEmail: string
    }
  | {
      /** Session title (run-prefixed question) — the Run-ID binding
       * proof for the server-generated session id. */
      credential: CredentialKind
      id: string
      kind: 'agent_session'
      ownerEmail: string
      title: string
    }
  | {
      /** Title of the owning agent session — the run binding travels
       * through the session (share->document cascade pattern). */
      credential: CredentialKind
      id: string
      kind: 'agent_run'
      ownerEmail: string
      sessionTitle: string
    }
  | {
      credential: CredentialKind
      id: string
      kind: 'research_run'
      ownerEmail: string
      question: string
    }
  | {
      /** Run-prefixed first question — the Run-ID binding proof for the
       * client-generated thread id. */
      credential: CredentialKind
      id: string
      kind: 'chat_thread'
      ownerEmail: string
      title: string
    }
  | {
      /** Run-prefixed template title — the Run-ID binding proof for the
       * server-generated template id. */
      credential: CredentialKind
      id: string
      kind: 'prompt_template'
      ownerEmail: string
      title: string
    }
  | {
      credential: CredentialKind
      id: string
      kind: 'knowledge_collection'
      name: string
      ownerEmail: string
    }
  | {
      /** Run-prefixed asset title — the Run-ID binding proof for the
       * client-chosen library asset id. Deleting the asset removes its
       * original file and the knowledge documents derived from it. */
      credential: CredentialKind
      id: string
      kind: 'asset'
      ownerEmail: string
      title: string
    }
  | {
      email: string
      id: string
      kind: 'temporary_user'
    }
  | {
      email: string
      id: string
      kind: 'temporary_user_project'
    }
  | {
      id: string
      kind: 'session'
      storageStatePath: string
    }
  | {
      composeProject: string
      containerId: string
      engine: 'podman'
      id: string
      kind: 'network_qdisc'
    }
) & { retainForReview?: boolean }

type LifecycleMessage =
  | {
      protocol: typeof PROTOCOL
      requestId: string
      resource: ProductResource
      runId: string
      type: 'register'
    }
  | {
      handleId: string
      protocol: typeof PROTOCOL
      requestId: string
      runId: string
      type: 'complete'
    }

export class ProductResourceController {
  private readonly context: RunContext
  private readonly handles = new Map<string, CleanupHandle>()
  private readonly ledger: CleanupLedger
  private readonly cleanup: ProductCleanup

  constructor(
    context: RunContext,
    ledger: CleanupLedger,
    cleanup: ProductCleanup = cleanupProductResource,
  ) {
    this.cleanup = cleanup
    this.context = context
    this.ledger = ledger
  }

  async handle(child: ChildProcess, value: unknown): Promise<void> {
    if (!isLifecycleMessage(value) || value.runId !== this.context.runId) return
    try {
      if (value.type === 'register') {
        const resource = validateResource(
          value.resource,
          this.context.runId,
          this.context.reportDirectory,
        )
        const handle = await this.ledger.register(
          'resource',
          resourceLabel(resource),
          async () => await this.cleanup(this.context, resource),
        )
        this.handles.set(handle.id, handle)
        child.send?.({
          handleId: handle.id,
          protocol: PROTOCOL,
          requestId: value.requestId,
          type: 'ack',
        })
        return
      }
      const handle = this.handles.get(value.handleId)
      if (!handle) throw new Error('Unknown product cleanup handle.')
      await this.ledger.complete(handle)
      child.send?.({
        handleId: handle.id,
        protocol: PROTOCOL,
        requestId: value.requestId,
        type: 'ack',
      })
    } catch {
      child.send?.({
        protocol: PROTOCOL,
        requestId: value.requestId,
        type: 'error',
      })
    }
  }
}

export type ProductCleanup = (
  context: RunContext,
  resource: ProductResource,
) => Promise<CleanupDisposition>

export function retainsReviewResource(context: Pick<RunContext, 'profile' | 'environment'>, resource: ProductResource): boolean {
  return context.profile === 'agent-desk'
    && context.environment.INQTRIX_E2E_RETAIN_REVIEW_DATA === '1'
    && resource.retainForReview === true
    && ['agent_session', 'agent_run', 'document', 'knowledge_collection', 'asset'].includes(resource.kind)
}

async function cleanupProductResource(
  context: RunContext,
  resource: ProductResource,
): Promise<CleanupDisposition> {
  const retain = retainsReviewResource(context, resource)
  if (retain && resource.kind !== 'agent_run') {
    const path = resource.kind === 'agent_session'
      ? `/v1/agent-sessions/${encodeURIComponent(resource.id)}`
      : resource.kind === 'document'
        ? `/v1/editor/documents/${encodeURIComponent(resource.id)}`
        : resource.kind === 'asset'
          ? `/v1/assets/${encodeURIComponent(resource.id)}`
          : '/v1/knowledge/collections'
    let exists = false
    if (!('ownerEmail' in resource) || !('credential' in resource)) {
      throw new Error('A review resource requires an owner.')
    }
    await withCleanupActor(context, resource.ownerEmail, resource.credential, async (actor) => {
      const response = await actorFetch(actor, 'GET', path, resource.kind === 'knowledge_collection' ? [200] : [200, 404])
      exists = response.status() === 200
      if (resource.kind === 'knowledge_collection') {
        const listing = await response.json()
        if (!Array.isArray(listing.data)) throw new Error('Knowledge collection listing is invalid.')
        const collection = listing.data.find((row: { id?: string }) => row.id === resource.id)
        exists = collection !== undefined
        if (exists && !runLabelBelongsToRun(collection.name, context.runId)) {
          throw new Error('The review collection no longer matches this verification run.')
        }
      }
      if (exists && resource.kind === 'agent_session') {
        const session = await response.json()
        if (!agentSessionBelongsToRun(session.title, context.runId)) {
          throw new Error('The review session no longer matches this verification run.')
        }
      }
      if (exists && resource.kind === 'asset') {
        const asset = await response.json()
        if (!runLabelBelongsToRun(asset.title, context.runId)) {
          throw new Error('The review asset no longer matches this verification run.')
        }
      }
    })
    return exists ? 'retained' : undefined
  }
  if (resource.kind === 'network_qdisc') {
    const containers = await resolveFaultControlContainers({
      engine: resource.engine,
      repositoryRoot: context.repositoryRoot,
    })
    if (
      containers.project !== resource.composeProject
      || containers.collaboration !== resource.containerId
    ) {
      throw new Error('Registered network qdisc no longer matches the canonical stack.')
    }
    await cleanupVerificationNetworkShape({
      containerId: resource.containerId,
      peerContainerId: containers.web,
      repositoryRoot: context.repositoryRoot,
    })
    return
  }
  if (resource.kind === 'session') {
    await cleanupStoredSession(context, resource.storageStatePath)
    return
  }
  if (resource.kind === 'agent_run' || resource.kind === 'research_run') {
    // An interrupted engine may not have reached its own finally block.
    // Cancel and settle the exact registered run before deleting it.
    let retained = false
    await withCleanupActor(
      context,
      resource.ownerEmail,
      resource.credential,
      async (actor) => {
        const path = `/v1/runs/${encodeURIComponent(resource.id)}`
        const initial = await actorFetch(actor, 'GET', path, [200, 404])
        if (initial.status() === 200) {
          const current = await initial.json()
          if (resource.kind === 'agent_run') {
            if (!nonEmpty(current.session_id)) {
              throw new Error('The registered agent run has no saved session.')
            }
            const session = await actorFetchJson(actor, 'GET',
              `/v1/agent-sessions/${encodeURIComponent(current.session_id)}`, [200])
            if (!agentSessionBelongsToRun(session.title, context.runId)) {
              throw new Error('The registered agent run belongs to a different session.')
            }
          }
          if (!['completed', 'failed', 'cancelled'].includes(current.status)) {
            await actorFetch(actor, 'POST', `${path}/cancel`, [200, 202, 404, 409])
            const until = Date.now() + 120_000
            let terminal = false
            while (Date.now() < until) {
              const response = await actorFetch(actor, 'GET', path, [200, 404])
              if (response.status() === 404) return
              if (['completed', 'failed', 'cancelled'].includes((await response.json()).status)) {
                terminal = true
                break
              }
              await new Promise((resolveWait) => setTimeout(resolveWait, 250))
            }
            if (!terminal) throw new Error('The owned run did not become terminal during cleanup.')
          }
          if (retain) {
            retained = true
            return
          }
        }
        await actorFetch(
          actor,
          'DELETE',
          `/v1/runs/${encodeURIComponent(resource.id)}`,
          [200, 204, 404],
        )
      },
    )
    return retained ? 'retained' : undefined
  }
  if (resource.kind === 'knowledge_collection') {
    await withCleanupActor(
      context,
      resource.ownerEmail,
      resource.credential,
      async (actor) => {
        const response = await actorFetch(
          actor,
          'DELETE',
          `/v1/knowledge/collections/${encodeURIComponent(resource.id)}`,
          [202, 204, 404],
        )
        if (response.status() === 202) {
          await awaitDeletionOperation(actor, await response.json())
        }
      },
    )
    return
  }
  if (resource.kind === 'asset') {
    // The asset aggregate owns its original file and derived knowledge
    // documents; its deletion is a durable operation like a collection's.
    await withCleanupActor(
      context,
      resource.ownerEmail,
      resource.credential,
      async (actor) => {
        const response = await actorFetch(
          actor,
          'DELETE',
          `/v1/assets/${encodeURIComponent(resource.id)}`,
          [202, 204, 404],
        )
        if (response.status() === 202) {
          await awaitDeletionOperation(actor, await response.json())
        }
      },
    )
    return
  }
  if (resource.kind === 'chat_thread') {
    // The engine deletes its thread through the visible UI; this pass is
    // the 404-tolerant crash-safety net (owner-only, idempotent).
    await withCleanupActor(
      context,
      resource.ownerEmail,
      resource.credential,
      async (actor) => {
        await actorFetch(
          actor,
          'DELETE',
          `/v1/chat/threads/${encodeURIComponent(resource.id)}`,
          [200, 204, 404],
        )
      },
    )
    return
  }
  if (resource.kind === 'prompt_template') {
    // Deleting the template revokes its shares server-side; 404 keeps
    // the pass idempotent after the visible UI deletion.
    await withCleanupActor(
      context,
      resource.ownerEmail,
      resource.credential,
      async (actor) => {
        await actorFetch(
          actor,
          'DELETE',
          `/v1/prompt-templates/${encodeURIComponent(resource.id)}`,
          [200, 204, 404],
        )
      },
    )
    return
  }
  if (resource.kind === 'agent_session') {
    await withCleanupActor(
      context,
      resource.ownerEmail,
      resource.credential,
      async (actor) => {
        const response = await actorFetch(
          actor,
          'DELETE',
          `/v1/agent-sessions/${encodeURIComponent(resource.id)}`,
          [202, 204, 404],
        )
        // 202 accepts the deletion, it does not perform it. The record only
        // counts as cleaned once its operation reached a terminal state.
        if (response.status() === 202) {
          await awaitDeletionOperation(actor, await response.json())
        }
      },
    )
    return
  }
  if (resource.kind === 'temporary_user') {
    await withCleanupActor(context, adminEmail(context), 'admin', (actor) =>
      eraseTemporaryAccount(actor, resource.email),
    )
    return
  }
  if (resource.kind === 'temporary_user_project') {
    await withCleanupActor(context, resource.email, 'user', async (actor) => {
      await cleanupOwnedProjectDocuments({
        async deleteDocument(documentId) {
          await actorFetch(
            actor,
            'DELETE',
            `/v1/editor/documents/${encodeURIComponent(documentId)}`,
            [204, 404],
          )
        },
        async fetchPage(cursor) {
          const parameters = new URLSearchParams({ limit: '200', scope: 'owned' })
          if (cursor) parameters.set('cursor', cursor)
          return await actorFetchJson(
            actor,
            'GET',
            `/v1/editor/documents?${parameters.toString()}`,
            [200],
          )
        },
      })
    })
    return
  }
  const documentId = resource.kind === 'document'
    ? resource.id
    : resource.documentId
  await withCleanupActor(
    context,
    resource.ownerEmail,
    resource.credential,
    async (actor) => {
      await actorFetch(
        actor,
        'DELETE',
        `/v1/editor/documents/${encodeURIComponent(documentId)}`,
        [204, 404],
      )
    },
  )
}

async function cleanupStoredSession(
  context: RunContext,
  configuredPath: string,
): Promise<void> {
  const path = resolveSafeSecretPath(context.reportDirectory, configuredPath)
  let api: APIRequestContext | null = null
  try {
    api = await request.newContext({
      baseURL: baseURL(context),
      ignoreHTTPSErrors: context.environment.INQTRIX_E2E_IGNORE_HTTPS_ERRORS === '1',
      storageState: path,
    })
    const session = await api.get('/api/auth/session')
    if (session.status() === 401) return
    if (session.status() !== 200) {
      throw new Error(`Cleanup session lookup returned HTTP ${session.status()}.`)
    }
    const body = await session.json() as {
      authenticated?: boolean
      csrf_token?: string
      project_namespace?: string
      user?: { id?: string }
    }
    if (body.authenticated === false) return
    if (!body.csrf_token) throw new Error('Cleanup session has no CSRF token.')
    const logout = await api.post('/api/auth/logout', {
      headers: { 'X-CSRF-Token': body.csrf_token },
    })
    if (![200, 401].includes(logout.status())) {
      throw new Error(`Cleanup logout returned HTTP ${logout.status()}.`)
    }
  } finally {
    await api?.dispose()
    await unlink(path).catch(() => undefined)
  }
}

async function withCleanupActor(
  context: RunContext,
  email: string,
  credential: CredentialKind,
  operation: (actor: CleanupActor) => Promise<void>,
): Promise<void> {
  const api = await request.newContext({
    baseURL: baseURL(context),
    ignoreHTTPSErrors: context.environment.INQTRIX_E2E_IGNORE_HTTPS_ERRORS === '1',
  })
  let csrf = ''
  try {
    const login = await api.post('/api/auth/login/local', {
      data: {
        email,
        password: credential === 'admin'
          ? requiredEnvironment(context, 'INQTRIX_E2E_ADMIN_PASSWORD')
          : requiredEnvironment(context, 'INQTRIX_E2E_USER_PASSWORD'),
      },
    })
    if (login.status() !== 200) {
      throw new Error(`Cleanup login returned HTTP ${login.status()}.`)
    }
    const response = await api.get('/api/auth/session')
    if (response.status() !== 200) {
      throw new Error(`Cleanup session lookup returned HTTP ${response.status()}.`)
    }
    const session = await response.json() as {
      csrf_token?: string
      project_namespace?: string
      user?: { id?: string }
    }
    if (
      !session.csrf_token
      || !session.project_namespace
      || !session.user?.id
    ) {
      throw new Error('Cleanup session is incomplete.')
    }
    csrf = session.csrf_token
    await operation({
      api,
      csrf,
      userId: session.user.id,
      workspaceId: session.project_namespace,
    })
  } finally {
    if (csrf) {
      await api.post('/api/auth/logout', {
        headers: { 'X-CSRF-Token': csrf },
      }).catch(() => undefined)
    }
    await api.dispose()
  }
}

export type CleanupActor = {
  api: APIRequestContext
  csrf: string
  userId: string
  workspaceId: string
}

/**
 * Erase the temporary account with *email* and return once it is gone. The
 * record only counts as cleaned then; the erasure belongs to no workspace,
 * so it is read without one.
 */
export async function eraseTemporaryAccount(
  actor: CleanupActor,
  email: string,
): Promise<void> {
  const listing = await actorFetchJson(actor, 'GET', '/v1/admin/users', [200])
  const user = listing.users?.find(
    (candidate: { email?: string }) => candidate.email === email,
  )
  if (!user?.id) return
  const response = await actorFetch(
    actor,
    'DELETE',
    `/v1/admin/users/${encodeURIComponent(user.id)}`,
    [202, 404],
  )
  if (response.status() === 202) {
    await awaitDeletionOperation(actor, await response.json(), {
      timeoutMs: ACCOUNT_ERASURE_TIMEOUT_MS,
      workspace: false,
    })
  }
}

async function awaitDeletionOperation(
  actor: CleanupActor,
  summary: { operation_id?: string },
  {
    timeoutMs = DELETION_TERMINAL_TIMEOUT_MS,
    workspace = true,
  }: { timeoutMs?: number; workspace?: boolean } = {},
): Promise<void> {
  const operationId = summary.operation_id
  if (!operationId) {
    throw new Error('Accepted deletion carries no operation id.')
  }
  const path = `/v1/deletion-operations/${encodeURIComponent(operationId)}`
  await waitUntil(
    async () => {
      const response = await actorFetch(actor, 'GET', path, [200, 404], { workspace })
      if (response.status() === 404) return true
      const operation = await response.json() as {
        error?: unknown
        status?: string
      }
      if (operation.status === 'delete_failed') {
        throw new Error(
          `Deletion operation ${operationId} failed: `
          + `${JSON.stringify(operation.error)}.`,
        )
      }
      return operation.status === 'deleted'
    },
    timeoutMs,
    `deletion operation ${operationId}`,
  )
}

async function waitUntil(
  predicate: () => Promise<boolean>,
  timeoutMs: number,
  label: string,
): Promise<void> {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    if (await predicate()) return
    await new Promise((resolve) => setTimeout(resolve, 100))
  }
  throw new Error(`Timed out waiting for ${label}.`)
}

/** One cleanup request. Playwright's request errors list every request
 * header, the session cookie and the CSRF token included: only the kind of
 * failure leaves. */
async function cleanupRequest(
  actor: CleanupActor,
  method: string,
  path: string,
  headers: Record<string, string>,
): Promise<APIResponse> {
  try {
    return await actor.api.fetch(path, { headers, method })
  } catch (error) {
    throw new Error(`Product cleanup request ${requestFailureKind(error)}.`)
  }
}

async function actorFetch(
  actor: CleanupActor,
  method: string,
  path: string,
  expected: readonly number[],
  { workspace = true }: { workspace?: boolean } = {},
): Promise<APIResponse> {
  const headers: Record<string, string> = {
    'X-CSRF-Token': actor.csrf,
    'X-Inqtrix-Expected-User-Id': actor.userId,
  }
  // `workspace: false` for a resource that belongs to no workspace, such as
  // an account erasure: a request that names one does not find it.
  if (workspace) headers['X-Inqtrix-Workspace-Id'] = actor.workspaceId
  const response = await cleanupRequest(actor, method, path, headers)
  if (!expected.includes(response.status())) {
    throw new Error(`Product cleanup returned HTTP ${response.status()}.`)
  }
  return response
}

async function actorFetchJson(
  actor: CleanupActor,
  method: string,
  path: string,
  expected: readonly number[],
): Promise<Record<string, any>> {
  const response = await cleanupRequest(actor, method, path, {
    'X-Inqtrix-Expected-User-Id': actor.userId,
    'X-Inqtrix-Workspace-Id': actor.workspaceId,
  })
  if (!expected.includes(response.status())) {
    throw new Error(`Product cleanup returned HTTP ${response.status()}.`)
  }
  return await response.json() as Record<string, any>
}

function validateResource(
  value: ProductResource,
  runId: string,
  reportDirectory: string,
): ProductResource {
  if (!value || typeof value !== 'object' || typeof value.kind !== 'string') {
    throw new Error('Invalid product cleanup resource.')
  }
  if (value.retainForReview !== undefined && typeof value.retainForReview !== 'boolean') {
    throw new Error('Review retention must be a boolean.')
  }
  if (value.kind === 'document') {
    assertRunDocument(value.id, runId)
    assertOwner(value)
  } else if (value.kind === 'share' || value.kind === 'guest_link') {
    assertRunDocument(value.documentId, runId)
    assertOwner(value)
    if (!nonEmpty(value.id)) throw new Error('Cleanup dependency has no ID.')
  } else if (value.kind === 'agent_session') {
    assertOwner(value)
    if (!nonEmpty(value.id)) throw new Error('Cleanup dependency has no ID.')
    if (!agentSessionBelongsToRun(value.title, runId)) {
      throw new Error('Agent-session cleanup is not Run-ID-bound.')
    }
  } else if (value.kind === 'agent_run') {
    assertOwner(value)
    if (!nonEmpty(value.id)) throw new Error('Cleanup dependency has no ID.')
    if (!agentSessionBelongsToRun(value.sessionTitle, runId)) {
      throw new Error('Agent-run cleanup is not Run-ID-bound.')
    }
  } else if (value.kind === 'research_run') {
    assertOwner(value)
    if (!nonEmpty(value.id)) throw new Error('Cleanup dependency has no ID.')
    if (!runLabelBelongsToRun(value.question, runId)) {
      throw new Error('Research-run cleanup is not Run-ID-bound.')
    }
  } else if (value.kind === 'knowledge_collection') {
    assertOwner(value)
    if (!nonEmpty(value.id)) throw new Error('Cleanup dependency has no ID.')
    if (!runLabelBelongsToRun(value.name, runId)) {
      throw new Error('Knowledge-collection cleanup is not Run-ID-bound.')
    }
  } else if (value.kind === 'asset') {
    assertOwner(value)
    if (!nonEmpty(value.id)) throw new Error('Cleanup dependency has no ID.')
    if (!runLabelBelongsToRun(value.title, runId)) {
      throw new Error('Asset cleanup is not Run-ID-bound.')
    }
  } else if (value.kind === 'chat_thread') {
    assertOwner(value)
    if (!nonEmpty(value.id)) throw new Error('Cleanup dependency has no ID.')
    if (!runLabelBelongsToRun(value.title, runId)) {
      throw new Error('Chat-thread cleanup is not Run-ID-bound.')
    }
  } else if (value.kind === 'prompt_template') {
    assertOwner(value)
    if (!nonEmpty(value.id)) throw new Error('Cleanup dependency has no ID.')
    if (!runLabelBelongsToRun(value.title, runId)) {
      throw new Error('Prompt-template cleanup is not Run-ID-bound.')
    }
  } else if (value.kind === 'temporary_user') {
    if (
      value.id !== `${runId}:${value.email}`
      || !temporaryUserBelongsToRun(value.email, runId)
    ) {
      throw new Error('Temporary-user cleanup is not Run-ID-bound.')
    }
  } else if (value.kind === 'temporary_user_project') {
    if (
      value.id !== `${runId}:${value.email}:project`
      || !temporaryUserBelongsToRun(value.email, runId)
    ) {
      throw new Error('Temporary-user project cleanup is not Run-ID-bound.')
    }
  } else if (value.kind === 'session') {
    if (!value.id.startsWith(`session-${runId}-`)) {
      throw new Error('Session cleanup is not Run-ID-bound.')
    }
    resolveSafeSecretPath(reportDirectory, value.storageStatePath)
  } else if (value.kind === 'network_qdisc') {
    if (
      value.engine !== 'podman'
      || !/^[a-f0-9]{64}$/i.test(value.containerId)
      || !/^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/.test(value.composeProject)
      || value.id !== `${runId}:network-qdisc:${value.containerId}`
    ) {
      throw new Error('Network-qdisc cleanup is not safely Run-ID-bound.')
    }
  } else {
    throw new Error('Unsupported product cleanup resource.')
  }
  return value
}

function assertOwner(
  value: { credential: CredentialKind; ownerEmail: string },
): void {
  if (
    !nonEmpty(value.ownerEmail)
    || !['admin', 'user'].includes(value.credential)
  ) {
    throw new Error('Product cleanup owner is invalid.')
  }
}

function assertRunDocument(id: string, runId: string): void {
  if (!documentBelongsToRun(id, runId)) {
    throw new Error('Document cleanup is not Run-ID-bound.')
  }
}

function resolveSafeSecretPath(reportDirectory: string, value: string): string {
  const path = isAbsolute(value) ? resolve(value) : resolve(reportDirectory, value)
  const secretDirectory = resolve(reportDirectory, '.cleanup-secrets')
  const label = relative(secretDirectory, path)
  if (!label || label.startsWith('..') || isAbsolute(label)) {
    throw new Error('Session cleanup state escaped its private directory.')
  }
  return path
}

function resourceLabel(resource: ProductResource): string {
  if (resource.kind === 'document') {
    return 'document fixture for current run'
  }
  if (resource.kind === 'share') {
    return 'share cascade for current-run document'
  }
  if (resource.kind === 'guest_link') {
    return 'guest-link cascade for current-run document'
  }
  if (resource.kind === 'agent_session') {
    return 'agent session fixture for current run'
  }
  if (resource.kind === 'agent_run') {
    return 'agent run for current-run session'
  }
  if (resource.kind === 'research_run') {
    return 'research run fixture for current run'
  }
  if (resource.kind === 'knowledge_collection') {
    return 'knowledge collection fixture for current run'
  }
  if (resource.kind === 'asset') {
    return 'library asset fixture for current run'
  }
  if (resource.kind === 'chat_thread') {
    return 'chat thread fixture for current run'
  }
  if (resource.kind === 'prompt_template') {
    return 'prompt template fixture for current run'
  }
  if (resource.kind === 'temporary_user') {
    return 'temporary user fixture for current run'
  }
  if (resource.kind === 'temporary_user_project') {
    return 'temporary user project for current run'
  }
  if (resource.kind === 'network_qdisc') {
    return 'verification-owned collaboration network qdisc'
  }
  return 'account session fixture for current run'
}

function baseURL(context: RunContext): string {
  const value = context.environment.INQTRIX_E2E_BASE_URL
    ?? 'http://127.0.0.1:8080'
  const parsed = new URL(value)
  if (
    !['http:', 'https:'].includes(parsed.protocol)
    || parsed.username
    || parsed.password
    || parsed.search
    || parsed.hash
  ) {
    throw new Error('Cleanup base URL is invalid.')
  }
  return parsed.origin
}

function adminEmail(context: RunContext): string {
  return requiredEnvironment(context, 'INQTRIX_E2E_ADMIN_EMAIL')
}

function requiredEnvironment(context: RunContext, name: string): string {
  const value = context.environment[name]?.trim()
  if (!value) throw new Error(`${name} is required for product cleanup.`)
  return value
}

function nonEmpty(value: unknown): value is string {
  return typeof value === 'string' && value.length > 0
}

function runLabelBelongsToRun(value: string, runId: string): boolean {
  return typeof value === 'string' && value.startsWith(`${runId} `)
}

function isLifecycleMessage(value: unknown): value is LifecycleMessage {
  if (!value || typeof value !== 'object') return false
  const candidate = value as Record<string, unknown>
  return candidate.protocol === PROTOCOL
    && typeof candidate.requestId === 'string'
    && typeof candidate.runId === 'string'
    && (
      (
        candidate.type === 'register'
        && Boolean(candidate.resource)
      )
      || (
        candidate.type === 'complete'
        && typeof candidate.handleId === 'string'
      )
    )
}
