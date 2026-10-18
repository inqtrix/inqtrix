import { isAbsolute, relative, resolve } from 'node:path'

import { assertVerificationRunId } from './run-scope.mjs'
import { writePrivateJsonFixture } from './private-json.mjs'

export const GENERATED_SYSTEM_SMOKE_CONTRACT =
  'inqtrix-generated-system-smoke-v1'
export const GENERATED_SYSTEM_SMOKE_TRANSPORT = 'python-gateway'

export function normalizeSystemSmokeBaseURL(value) {
  let parsed
  try {
    parsed = new URL(value ?? 'http://127.0.0.1:8080')
  } catch {
    throw new Error('INQTRIX_E2E_BASE_URL must be a valid HTTP(S) URL.')
  }
  if (
    !['http:', 'https:'].includes(parsed.protocol)
    || parsed.username
    || parsed.password
    || parsed.search
    || parsed.hash
  ) {
    throw new Error(
      'INQTRIX_E2E_BASE_URL must be a credential-free HTTP(S) URL.',
    )
  }
  return parsed.origin
}

export function buildGeneratedSystemSmokeFixture({
  baseURL,
  collaborator,
  documents,
  owner,
  runId,
}) {
  assertVerificationRunId(runId)
  const normalizedOwner = requireActor(owner, 'owner')
  const normalizedCollaborator = requireActor(collaborator, 'collaborator')
  if (normalizedOwner.userId === normalizedCollaborator.userId) {
    throw new Error('Generated collaboration identities must be distinct.')
  }
  if (
    normalizedOwner.storageState
    === normalizedCollaborator.storageState
  ) {
    throw new Error(
      'Generated collaboration storage-state files must be distinct.',
    )
  }
  const directEdit = requireDocument(documents?.directEdit, 'directEdit')
  const concurrent = requireDocument(documents?.concurrent, 'concurrent')
  const detachedTransfer = requireDocument(
    documents?.detachedTransfer,
    'detachedTransfer',
  )
  const ime = requireDocument(documents?.ime, 'ime')
  const largeState = requireDocument(documents?.largeState, 'largeState')
  const layout = requireDocument(documents?.layout, 'layout')
  const mobileDrawers = requireDocument(documents?.mobileDrawers, 'mobileDrawers')
  const remotePresence = requireDocument(documents?.remotePresence, 'remotePresence')
  const revocation = requireDocument(documents?.revocation, 'revocation')
  const sourceReadonly = requireDocument(documents?.sourceReadonly, 'sourceReadonly')
  const staysConnected = requireDocument(documents?.staysConnected, 'staysConnected')
  const aiSuggestion = requireDocument(documents?.aiSuggestion, 'aiSuggestion')
  const suggestion = requireDocument(documents?.suggestion, 'suggestion')
  const suggestionUndo = requireDocument(documents?.suggestionUndo, 'suggestionUndo')
  const documentIds = [
    directEdit,
    concurrent,
    detachedTransfer,
    ime,
    largeState,
    layout,
    mobileDrawers,
    remotePresence,
    revocation,
    sourceReadonly,
    staysConnected,
    aiSuggestion,
    suggestion,
    suggestionUndo,
  ]
  if (new Set(documentIds).size !== documentIds.length) {
    throw new Error('Generated collaboration documents must be distinct.')
  }

  return {
    documents: {
      concurrent,
      detachedTransfer,
      directEdit,
      ime,
      largeState,
      layout,
      mobileDrawers,
      remotePresence,
      revocation,
      sourceReadonly,
      staysConnected,
      aiSuggestion,
      suggestion: {
        documentId: suggestion,
        expectedAuthorId: normalizedCollaborator.userId,
        expectedPermission: 'suggest',
      },
      suggestionUndo,
    },
    execution: {
      contract: GENERATED_SYSTEM_SMOKE_CONTRACT,
      runId,
      transport: GENERATED_SYSTEM_SMOKE_TRANSPORT,
    },
    locale: 'de',
    transports: {
      [GENERATED_SYSTEM_SMOKE_TRANSPORT]: {
        baseURL: normalizeSystemSmokeBaseURL(baseURL),
      },
    },
    users: {
      collaborator: {
        displayName: normalizedCollaborator.displayName,
        storageState: normalizedCollaborator.storageState,
        userId: normalizedCollaborator.userId,
      },
      owner: {
        displayName: normalizedOwner.displayName,
        storageState: normalizedOwner.storageState,
        userId: normalizedOwner.userId,
      },
    },
    version: 2,
  }
}

export async function writeGeneratedSystemSmokeFixture(path, fixture) {
  await writePrivateJsonFixture(path, fixture)
}

export function fixtureIsInsidePrivateRunDirectory(
  fixturePath,
  reportDirectory,
) {
  if (
    typeof fixturePath !== 'string'
    || typeof reportDirectory !== 'string'
    || !fixturePath
    || !reportDirectory
  ) return false
  const privateDirectory = resolve(reportDirectory, '.cleanup-secrets')
  const target = resolve(fixturePath)
  const pathFromPrivateDirectory = relative(privateDirectory, target)
  return (
    pathFromPrivateDirectory.length > 0
    && !pathFromPrivateDirectory.startsWith('..')
    && !isAbsolute(pathFromPrivateDirectory)
  )
}

function requireActor(value, label) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error(`Generated ${label} identity is invalid.`)
  }
  for (const field of ['displayName', 'storageState', 'userId']) {
    if (typeof value[field] !== 'string' || !value[field].trim()) {
      throw new Error(`Generated ${label} identity has no ${field}.`)
    }
  }
  if (!isUuid(value.userId)) {
    throw new Error(`Generated ${label} identity has an invalid userId.`)
  }
  return {
    displayName: value.displayName.trim(),
    storageState: resolve(value.storageState),
    userId: value.userId,
  }
}

function requireDocument(value, label) {
  const id = typeof value === 'string'
    ? value
    : value && typeof value === 'object' && !Array.isArray(value)
      ? value.id
      : null
  if (typeof id !== 'string' || !id.trim()) {
    throw new Error(`Generated ${label} document is invalid.`)
  }
  return id
}

function isUuid(value) {
  return /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i
    .test(value)
}
