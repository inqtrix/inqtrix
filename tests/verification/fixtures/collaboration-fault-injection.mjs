import { resolve } from 'node:path'

import {
  fixtureIsInsidePrivateRunDirectory,
  normalizeSystemSmokeBaseURL,
} from './collaboration-system-smoke.mjs'
import { writePrivateJsonFixture } from './private-json.mjs'
import { assertVerificationRunId } from './run-scope.mjs'

export const GENERATED_FAULT_INJECTION_CONTRACT =
  'inqtrix-generated-fault-injection-v1'
export const GENERATED_FAULT_INJECTION_TRANSPORT = 'python-gateway'
export const GENERATED_FAULT_INJECTION_PRIVATE_ANCHOR_TARGETS = [
  'chromium-desktop',
  'chromium-mobile',
  'firefox-desktop',
  'webkit-desktop',
]

export function buildGeneratedFaultInjectionFixture({
  baseURL,
  collaborator,
  controls,
  documents,
  owner,
  privateAnchors,
  runId,
}) {
  assertVerificationRunId(runId)
  const normalizedOwner = requireActor(owner, 'owner')
  const normalizedCollaborator = requireActor(collaborator, 'collaborator')
  if (normalizedOwner.userId === normalizedCollaborator.userId) {
    throw new Error('Generated collaboration identities must be distinct.')
  }
  if (normalizedOwner.storageState === normalizedCollaborator.storageState) {
    throw new Error(
      'Generated collaboration storage-state files must be distinct.',
    )
  }

  const normalizedDocuments = Object.fromEntries([
    'directEdit',
    'downgrade',
    'gatewayOutage',
    'outage',
    'protocol',
    'reconciliation',
    'revocation',
    'suggestion',
  ].map((key) => [key, requireDocument(documents?.[key], key)]))
  const privateAnchorDocuments = Object.fromEntries(
    GENERATED_FAULT_INJECTION_PRIVATE_ANCHOR_TARGETS.map((target) => [
      target,
      requireDocument(
        privateAnchors?.documents?.[target],
        `privateAnchors.documents.${target}`,
      ),
    ]),
  )
  const documentIds = [
    ...Object.values(normalizedDocuments),
    ...Object.values(privateAnchorDocuments),
  ]
  if (new Set(documentIds).size !== documentIds.length) {
    throw new Error('Generated collaboration documents must be distinct.')
  }

  const normalizedControls = requireControls(controls, runId)
  const normalizedPrivateAnchors = {
    collaborator: requirePrivateAnchorActor(
      privateAnchors?.collaborator,
      'collaborator',
    ),
    documents: privateAnchorDocuments,
    owner: requirePrivateAnchorActor(privateAnchors?.owner, 'owner'),
  }

  return {
    controls: normalizedControls,
    documents: {
      directEdit: normalizedDocuments.directEdit,
      downgrade: normalizedDocuments.downgrade,
      gatewayOutage: normalizedDocuments.gatewayOutage,
      outage: normalizedDocuments.outage,
      protocol: normalizedDocuments.protocol,
      reconciliation: normalizedDocuments.reconciliation,
      revocation: normalizedDocuments.revocation,
      suggestion: {
        documentId: normalizedDocuments.suggestion,
        expectedAuthorId: normalizedCollaborator.userId,
        expectedPermission: 'suggest',
      },
    },
    execution: {
      contract: GENERATED_FAULT_INJECTION_CONTRACT,
      runId,
      transport: GENERATED_FAULT_INJECTION_TRANSPORT,
    },
    locale: 'de',
    privateAnchors: normalizedPrivateAnchors,
    transports: {
      [GENERATED_FAULT_INJECTION_TRANSPORT]: {
        baseURL: normalizeSystemSmokeBaseURL(baseURL),
      },
    },
    users: {
      collaborator: normalizedCollaborator,
      owner: normalizedOwner,
    },
    version: 2,
  }
}

export async function writeGeneratedFaultInjectionFixture(path, fixture) {
  await writePrivateJsonFixture(path, fixture)
}

export { fixtureIsInsidePrivateRunDirectory }

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
  return id.trim()
}

function requireControls(value, runId) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error('Generated fault controls are invalid.')
  }
  const baseURL = normalizeLoopbackControlURL(value.baseURL)
  const paths = value.paths
  if (!paths || typeof paths !== 'object' || Array.isArray(paths)) {
    throw new Error('Generated fault-control paths are invalid.')
  }
  const result = {
    armGatewayOutagePath: requireControlPath(
      paths.armGatewayOutage,
      'armGatewayOutage',
    ),
    armLostAckPath: requireControlPath(paths.armLostAck, 'armLostAck'),
    armOutagePath: requireControlPath(paths.armOutage, 'armOutage'),
    authorizationEnv: requireString(
      value.authorizationEnv,
      'fault-control authorizationEnv',
    ),
    baseURL,
    operationStatusPath: requireControlPath(
      paths.operationStatus,
      'operationStatus',
    ),
    restartPath: requireControlPath(paths.restart, 'restart'),
    restorePath: requireControlPath(paths.restore, 'restore'),
    runId,
  }
  if (!/^[A-Z][A-Z0-9_]*$/.test(result.authorizationEnv)) {
    throw new Error(
      'Generated fault-control authorizationEnv must name an uppercase environment variable.',
    )
  }
  return result
}

function requirePrivateAnchorActor(value, label) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error(`Generated ${label} private anchors are invalid.`)
  }
  return {
    aiAnchorText: requireString(value.aiAnchorText, `${label} aiAnchorText`),
    aiInstructionText: requireString(
      value.aiInstructionText,
      `${label} aiInstructionText`,
    ),
    aiText: requireString(value.aiText, `${label} aiText`),
    commentAnchorText: requireString(
      value.commentAnchorText,
      `${label} commentAnchorText`,
    ),
    commentText: requireString(value.commentText, `${label} commentText`),
  }
}

function normalizeLoopbackControlURL(value) {
  const parsed = new URL(requireString(value, 'fault-control baseURL'))
  if (
    parsed.protocol !== 'http:'
    || parsed.hostname !== '127.0.0.1'
    || parsed.username
    || parsed.password
    || parsed.search
    || parsed.hash
    || parsed.pathname !== '/'
  ) {
    throw new Error(
      'Generated fault-control baseURL must be an uncredentialed 127.0.0.1 HTTP origin.',
    )
  }
  return parsed.origin
}

function requireControlPath(value, label) {
  const path = requireString(value, `fault-control ${label} path`)
  if (
    !path.startsWith('/')
    || path.startsWith('//')
    || path.includes('?')
    || path.includes('#')
  ) {
    throw new Error(`Generated fault-control ${label} path is invalid.`)
  }
  return path
}

function requireString(value, label) {
  if (typeof value !== 'string' || !value.trim()) {
    throw new Error(`Generated ${label} is invalid.`)
  }
  return value.trim()
}

function isUuid(value) {
  return /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i
    .test(value)
}
