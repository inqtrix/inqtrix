import { existsSync, readFileSync, statSync } from 'node:fs'
import { dirname, isAbsolute, resolve } from 'node:path'

import type { VerificationProfile } from '../tests/verification/model.ts'
import {
  GENERATED_FAULT_INJECTION_CONTRACT,
  GENERATED_FAULT_INJECTION_PRIVATE_ANCHOR_TARGETS,
  GENERATED_FAULT_INJECTION_TRANSPORT,
} from '../tests/verification/fixtures/collaboration-fault-injection.mjs'
import {
  fixtureIsInsidePrivateRunDirectory,
  GENERATED_SYSTEM_SMOKE_CONTRACT,
  GENERATED_SYSTEM_SMOKE_TRANSPORT,
} from '../tests/verification/fixtures/collaboration-system-smoke.mjs'

export const COLLABORATION_TRANSPORTS = [
  'vite',
  'nginx',
  'python-gateway',
] as const
export const COLLABORATION_E2E_MODES = ['dev', 'strict'] as const

export type CollaborationTransport = typeof COLLABORATION_TRANSPORTS[number]
export type CollaborationE2EMode = typeof COLLABORATION_E2E_MODES[number]
export type CollaborationLocale = 'de' | 'en'
type PrivateAnchorTarget =
  typeof GENERATED_FAULT_INJECTION_PRIVATE_ANCHOR_TARGETS[number]

type Environment = Record<string, string | undefined>

type RawUser = {
  displayName?: unknown
  storageState?: unknown
  userId?: unknown
}

type RawTransport = {
  baseURL?: unknown
  collaboratorStorageState?: unknown
  ownerStorageState?: unknown
}

type RawSuggestionDocument = {
  documentId?: unknown
  expectedAuthorId?: unknown
  expectedPermission?: unknown
}

type RawControls = {
  armGatewayOutagePath?: unknown
  armLostAckPath?: unknown
  armOutagePath?: unknown
  authorizationEnv?: unknown
  baseURL?: unknown
  operationStatusPath?: unknown
  restartPath?: unknown
  restorePath?: unknown
  runId?: unknown
}

type RawExecution = {
  contract?: unknown
  runId?: unknown
  transport?: unknown
}

type RawFixture = {
  controls?: RawControls
  documents?: {
    concurrent?: unknown
    detachedTransfer?: unknown
    directEdit?: unknown
    downgrade?: unknown
    gatewayOutage?: unknown
    ime?: unknown
    largeState?: unknown
    layout?: unknown
    mobileDrawers?: unknown
    outage?: unknown
    protocol?: unknown
    reconciliation?: unknown
    remotePresence?: unknown
    revocation?: unknown
    sourceReadonly?: unknown
    staysConnected?: unknown
    aiSuggestion?: unknown
    suggestion?: RawSuggestionDocument
    suggestionUndo?: unknown
  }
  execution?: RawExecution
  locale?: unknown
  privateAnchors?: {
    collaborator?: {
      aiAnchorText?: unknown
      aiInstructionText?: unknown
      aiText?: unknown
      commentAnchorText?: unknown
      commentText?: unknown
    }
    documents?: Partial<Record<PrivateAnchorTarget, unknown>>
    owner?: {
      aiAnchorText?: unknown
      aiInstructionText?: unknown
      aiText?: unknown
      commentAnchorText?: unknown
      commentText?: unknown
    }
  }
  transports?: Partial<Record<CollaborationTransport, RawTransport>>
  users?: {
    collaborator?: RawUser
    owner?: RawUser
  }
  version?: unknown
}

export type CollaborationControlFixture = {
  authorizationEnv: string
  baseURL: string
  paths: {
    armGatewayOutage: string
    armLostAck: string
    armOutage: string
    operationStatus: string
    restart: string
    restore: string
  }
  runId: string | null
}

export type CollaborationE2EStack = {
  capabilityReasons: {
    controls: string[]
    privateAnchors: string[]
  }
  collaborator: {
    displayName: string
    storageState: string
    userId: string
  }
  controls: CollaborationControlFixture | null
  documents: {
    concurrent: string | null
    detachedTransfer: string | null
    directEdit: string
    downgrade: string | null
    gatewayOutage: string | null
    ime: string
    largeState: string | null
    layout: string
    mobileDrawers: string
    outage: string | null
    protocol: string | null
    reconciliation: string | null
    remotePresence: string
    revocation: string
    sourceReadonly: string
    staysConnected: string
    aiSuggestion: string
    suggestion: {
      documentId: string
      expectedAuthorId: string
      expectedPermission: 'suggest'
    }
    suggestionUndo: string
  }
  locale: CollaborationLocale
  owner: {
    displayName: string
    storageState: string
    userId: string
  }
  privateAnchors: {
    collaborator: {
      aiAnchorText: string
      aiInstructionText: string | null
      aiText: string
      commentAnchorText: string
      commentText: string
    }
    documents: Record<PrivateAnchorTarget, string>
    owner: {
      aiAnchorText: string
      aiInstructionText: string | null
      aiText: string
      commentAnchorText: string
      commentText: string
    }
  } | null
  transports: Record<CollaborationTransport, {
    baseURL: string | null
    collaboratorStorageState: string
    ownerStorageState: string
    reasons: string[]
  }>
}

export type CollaborationE2EConfiguration = {
  mode: CollaborationE2EMode
  reasons: string[]
  selectedTransports: CollaborationTransport[]
  stack: CollaborationE2EStack | null
}

const TRANSPORT_ENV: Record<CollaborationTransport, string> = {
  nginx: 'INQTRIX_E2E_NGINX_BASE_URL',
  'python-gateway': 'INQTRIX_E2E_PYTHON_GATEWAY_BASE_URL',
  vite: 'INQTRIX_E2E_VITE_BASE_URL',
}

export function resolveCollaborationE2EMode(
  environment: Environment = process.env,
): CollaborationE2EMode {
  const value = environment.INQTRIX_E2E_MODE ?? 'dev'
  if (value === 'dev' || value === 'strict') return value
  throw new Error('INQTRIX_E2E_MODE must be "dev" or "strict".')
}

export function loadCollaborationE2EConfiguration(
  environment: Environment = process.env,
  workingDirectory = process.cwd(),
): CollaborationE2EConfiguration {
  const mode = resolveCollaborationE2EMode(environment)
  const fixturePath = environment.INQTRIX_E2E_FIXTURE
  if (!fixturePath) {
    return {
      mode,
      reasons: ['INQTRIX_E2E_FIXTURE is not set'],
      selectedTransports: [...COLLABORATION_TRANSPORTS],
      stack: null,
    }
  }

  const absoluteFixturePath = resolve(workingDirectory, fixturePath)
  if (!existsSync(absoluteFixturePath)) {
    return {
      mode,
      reasons: ['INQTRIX_E2E_FIXTURE does not exist'],
      selectedTransports: [...COLLABORATION_TRANSPORTS],
      stack: null,
    }
  }

  let raw: RawFixture
  try {
    raw = JSON.parse(readFileSync(absoluteFixturePath, 'utf8')) as RawFixture
  } catch {
    return {
      mode,
      reasons: ['INQTRIX_E2E_FIXTURE is not valid JSON'],
      selectedTransports: [...COLLABORATION_TRANSPORTS],
      stack: null,
    }
  }

  const reasons: string[] = []
  const selectedTransports = parseSelectedTransports(
    raw.execution,
    absoluteFixturePath,
    environment,
    reasons,
  )
  if (raw.version !== 2) reasons.push('fixture.version must equal 2')
  const locale = raw.locale === 'de' || raw.locale === 'en' ? raw.locale : null
  if (!locale) reasons.push('fixture.locale must be "de" or "en"')

  const owner = parseUser(
    raw.users?.owner,
    'fixture.users.owner',
    absoluteFixturePath,
    reasons,
  )
  const collaborator = parseUser(
    raw.users?.collaborator,
    'fixture.users.collaborator',
    absoluteFixturePath,
    reasons,
  )
  if (owner && collaborator) {
    if (owner.userId === collaborator.userId) {
      reasons.push('fixture owner and collaborator user IDs must be distinct')
    }
    if (owner.storageState === collaborator.storageState) {
      reasons.push('fixture owner and collaborator storageState files must be distinct')
    }
  }
  const documents = {
    concurrent: optionalCapabilityString(raw.documents?.concurrent),
    detachedTransfer: optionalCapabilityString(raw.documents?.detachedTransfer),
    directEdit: requiredString(raw.documents?.directEdit, 'fixture.documents.directEdit', reasons),
    downgrade: optionalCapabilityString(raw.documents?.downgrade),
    gatewayOutage: optionalCapabilityString(raw.documents?.gatewayOutage),
    ime: optionalCapabilityString(raw.documents?.ime),
    largeState: optionalCapabilityString(raw.documents?.largeState),
    layout: optionalCapabilityString(raw.documents?.layout),
    mobileDrawers: optionalCapabilityString(raw.documents?.mobileDrawers),
    outage: optionalCapabilityString(raw.documents?.outage),
    protocol: optionalCapabilityString(raw.documents?.protocol),
    reconciliation: optionalCapabilityString(raw.documents?.reconciliation),
    remotePresence: optionalCapabilityString(raw.documents?.remotePresence),
    revocation: requiredString(raw.documents?.revocation, 'fixture.documents.revocation', reasons),
    sourceReadonly: optionalCapabilityString(raw.documents?.sourceReadonly),
    staysConnected: optionalCapabilityString(raw.documents?.staysConnected),
    aiSuggestion: optionalCapabilityString(raw.documents?.aiSuggestion),
    suggestion: parseSuggestionDocument(raw.documents?.suggestion, reasons),
    suggestionUndo: optionalCapabilityString(raw.documents?.suggestionUndo),
  }
  if (
    collaborator
    && documents.suggestion
    && documents.suggestion.expectedAuthorId !== collaborator.userId
  ) {
    reasons.push(
      'fixture.documents.suggestion.expectedAuthorId must equal fixture.users.collaborator.userId',
    )
  }
  const privateReasons: string[] = []
  const privateAnchors = parsePrivateAnchors(raw.privateAnchors, privateReasons)
  const controlReasons: string[] = []
  const controls = parseControls(
    raw.controls,
    controlReasons,
    environment,
    raw.execution?.contract === GENERATED_FAULT_INJECTION_CONTRACT,
  )
  const suggestion = documents.suggestion

  if (
    !owner
    || !collaborator
    || !locale
    || !documents.directEdit
    || !documents.revocation
    || !suggestion
  ) {
    return { mode, reasons, selectedTransports, stack: null }
  }

  const fixtureDirectory = dirname(absoluteFixturePath)
  const transports = Object.fromEntries(COLLABORATION_TRANSPORTS.map((transport) => {
    const rawTransport = raw.transports?.[transport]
    const endpoint = nonEmptyString(environment[TRANSPORT_ENV[transport]])
      ?? nonEmptyString(rawTransport?.baseURL)
    const transportReasons: string[] = []
    if (!endpoint) {
      transportReasons.push(
        `${TRANSPORT_ENV[transport]} is not set and fixture.transports.${transport}.baseURL is absent`,
      )
    } else if (!isHttpUrl(endpoint)) {
      transportReasons.push(`${transport} baseURL must use http:// or https://`)
    } else if (hasUrlSecrets(endpoint)) {
      transportReasons.push(
        `${transport} baseURL must not contain credentials, query, or fragment data`,
      )
    }

    const ownerOverride = resolveOptionalPath(rawTransport?.ownerStorageState, fixtureDirectory)
    const collaboratorOverride = resolveOptionalPath(
      rawTransport?.collaboratorStorageState,
      fixtureDirectory,
    )
    const ownerState = ownerOverride ?? owner.storageState
    const collaboratorState = collaboratorOverride ?? collaborator.storageState
    if (!existsSync(ownerState)) {
      transportReasons.push(`${transport} owner storageState does not exist`)
    } else if (!privateCredentialFile(ownerState)) {
      transportReasons.push(
        `${transport} owner storageState must not be accessible by group or other users`,
      )
    }
    if (!existsSync(collaboratorState)) {
      transportReasons.push(`${transport} collaborator storageState does not exist`)
    } else if (!privateCredentialFile(collaboratorState)) {
      transportReasons.push(
        `${transport} collaborator storageState must not be accessible by group or other users`,
      )
    }
    if (ownerState === collaboratorState) {
      transportReasons.push(
        `${transport} owner and collaborator storageState files must be distinct`,
      )
    }

    return [transport, {
      baseURL: endpoint && isHttpUrl(endpoint) && !hasUrlSecrets(endpoint)
        ? normalizeHttpUrl(endpoint)
        : null,
      collaboratorStorageState: collaboratorState,
      ownerStorageState: ownerState,
      reasons: transportReasons,
    }]
  })) as CollaborationE2EStack['transports']

  return {
    mode,
    reasons,
    selectedTransports,
    stack: {
      capabilityReasons: {
        controls: controlReasons,
        privateAnchors: privateReasons,
      },
      collaborator,
      controls,
      documents: {
        ...documents,
        ime: documents.ime ?? suggestion.documentId,
        layout: documents.layout ?? suggestion.documentId,
        mobileDrawers: documents.mobileDrawers ?? documents.directEdit,
        remotePresence: documents.remotePresence ?? documents.directEdit,
        sourceReadonly: documents.sourceReadonly ?? documents.directEdit,
        // Rueckfall bewusst auf concurrent, NICHT auf directEdit: dort
        // laedt ein Szenario die Seite absichtlich neu, und der
        // Stabilitaetstest liefe in den Deckel von fuenf gleichzeitigen
        // Sitzungen je Nutzer und Dokument.
        staysConnected: documents.staysConnected
          ?? documents.concurrent
          ?? documents.directEdit,
        aiSuggestion: documents.aiSuggestion ?? documents.directEdit,
        suggestion,
        suggestionUndo: documents.suggestionUndo ?? documents.directEdit,
      },
      locale,
      owner,
      privateAnchors,
      transports,
    },
  }
}

/** Why the guest-link section cannot run, or `null` when it can.
 *
 * Guest links depend on a trusted secure context — Secure cookies, an
 * HTTPS origin and WSS — so an HTTP stack cannot exercise them at all.
 * Deciding that here, before the six-user matrix starts, keeps an
 * environment precondition from surfacing as a crash minutes later that
 * takes every already-passed scenario down with it.
 *
 * The two reasons stay distinct on purpose: a deployment that does not
 * offer guest links is a different statement from one that offers them
 * while this environment cannot reach them. Neither may ever be
 * reported as coverage.
 */
export function guestLinkGateReason(
  baseURL: string,
  capabilityEnabled: boolean,
): string | null {
  if (!capabilityEnabled) return 'editor guest links are not enabled'
  let protocol: string
  try {
    protocol = new URL(baseURL).protocol
  } catch {
    return `guest-link gate needs a parsable base URL, received ${baseURL}`
  }
  if (protocol !== 'https:') {
    return 'guest-link gate requires an HTTPS base URL; this stack serves HTTP'
  }
  return null
}

export function strictPreflightReasons(
  configuration: CollaborationE2EConfiguration,
  profile: VerificationProfile,
  environment: Environment = process.env,
): string[] {
  const reasons = [...configuration.reasons]
  const { stack } = configuration
  if (!stack) return reasons.length > 0 ? reasons : ['collaboration stack is unavailable']

  for (const transport of configuration.selectedTransports) {
    reasons.push(...stack.transports[transport].reasons)
  }
  const endpoints = configuration.selectedTransports
    .map((transport) => stack.transports[transport].baseURL)
    .filter((value): value is string => value !== null)
  const origins = endpoints.map((endpoint) => new URL(endpoint).origin)
  if (
    configuration.selectedTransports.length === COLLABORATION_TRANSPORTS.length
    && origins.length === COLLABORATION_TRANSPORTS.length
    && new Set(origins).size !== origins.length
  ) {
    reasons.push(
      'vite, nginx, and python-gateway base URLs must use three distinct origins',
    )
  }
  if (profile === 'system-smoke') {
    if (!stack.documents.concurrent) {
      reasons.push('fixture.documents.concurrent is required for system-smoke')
    }
    if (!stack.documents.detachedTransfer) {
      reasons.push('fixture.documents.detachedTransfer is required for system-smoke')
    }
    if (!stack.documents.largeState) {
      reasons.push('fixture.documents.largeState is required for system-smoke')
    }
  }
  if (profile === 'fault-injection') {
    if (!stack.documents.downgrade) {
      reasons.push('fixture.documents.downgrade is required for fault-injection')
    }
    if (!stack.documents.gatewayOutage) {
      reasons.push('fixture.documents.gatewayOutage is required for fault-injection')
    }
    if (!stack.documents.reconciliation) {
      reasons.push('fixture.documents.reconciliation is required for fault-injection')
    }
    if (!stack.documents.outage) {
      reasons.push('fixture.documents.outage is required for fault-injection')
    }
    if (!stack.documents.protocol) {
      reasons.push('fixture.documents.protocol is required for fault-injection')
    }
    reasons.push(...stack.capabilityReasons.privateAnchors)
    if (!stack.privateAnchors) {
      reasons.push('fixture.privateAnchors is required for fault-injection')
    }
    reasons.push(...stack.capabilityReasons.controls)
    if (!stack.controls) {
      reasons.push('fixture.controls is required for fault-injection')
    } else if (!nonEmptyString(environment[stack.controls.authorizationEnv])) {
      reasons.push(
        `${stack.controls.authorizationEnv} is required for fault-control authorization`,
      )
    }
  }
  return uniqueStrings(reasons)
}

export function assertStrictE2EConfiguration(
  configuration: CollaborationE2EConfiguration,
  profile: VerificationProfile,
  environment: Environment = process.env,
): void {
  if (configuration.mode !== 'strict') return
  if (profile !== 'system-smoke' && profile !== 'fault-injection') {
    throw new Error(
      'Strict collaboration E2E requires system-smoke or fault-injection profile.',
    )
  }
  const reasons = strictPreflightReasons(configuration, profile, environment)
  if (reasons.length === 0) return
  throw new Error(
    `Collaboration ${profile} preflight failed:\n- ${reasons.join('\n- ')}`,
  )
}

function parsePrivateAnchors(
  raw: RawFixture['privateAnchors'],
  reasons: string[],
): CollaborationE2EStack['privateAnchors'] {
  if (raw === undefined) return null
  const documents = Object.fromEntries(
    GENERATED_FAULT_INJECTION_PRIVATE_ANCHOR_TARGETS.map((target) => [
      target,
      requiredString(
        raw.documents?.[target],
        `fixture.privateAnchors.documents.${target}`,
        reasons,
      ),
    ]),
  ) as Record<PrivateAnchorTarget, string | null>
  const ownerAiAnchor = requiredString(
    raw.owner?.aiAnchorText,
    'fixture.privateAnchors.owner.aiAnchorText',
    reasons,
  )
  const ownerAi = requiredString(raw.owner?.aiText, 'fixture.privateAnchors.owner.aiText', reasons)
  const ownerAiInstruction = optionalCapabilityString(
    raw.owner?.aiInstructionText,
  )
  const ownerCommentAnchor = requiredString(
    raw.owner?.commentAnchorText,
    'fixture.privateAnchors.owner.commentAnchorText',
    reasons,
  )
  const ownerComment = requiredString(
    raw.owner?.commentText,
    'fixture.privateAnchors.owner.commentText',
    reasons,
  )
  const collaboratorAi = requiredString(
    raw.collaborator?.aiText,
    'fixture.privateAnchors.collaborator.aiText',
    reasons,
  )
  const collaboratorAiInstruction = optionalCapabilityString(
    raw.collaborator?.aiInstructionText,
  )
  const collaboratorAiAnchor = requiredString(
    raw.collaborator?.aiAnchorText,
    'fixture.privateAnchors.collaborator.aiAnchorText',
    reasons,
  )
  const collaboratorCommentAnchor = requiredString(
    raw.collaborator?.commentAnchorText,
    'fixture.privateAnchors.collaborator.commentAnchorText',
    reasons,
  )
  const collaboratorComment = requiredString(
    raw.collaborator?.commentText,
    'fixture.privateAnchors.collaborator.commentText',
    reasons,
  )
  if (
    Object.values(documents).some((documentId) => !documentId)
    || !ownerAiAnchor
    || !ownerAi
    || !ownerCommentAnchor
    || !ownerComment
    || !collaboratorAiAnchor
    || !collaboratorAi
    || !collaboratorCommentAnchor
    || !collaboratorComment
  ) return null
  return {
    collaborator: {
      aiAnchorText: collaboratorAiAnchor,
      aiInstructionText: collaboratorAiInstruction,
      aiText: collaboratorAi,
      commentAnchorText: collaboratorCommentAnchor,
      commentText: collaboratorComment,
    },
    documents: documents as Record<PrivateAnchorTarget, string>,
    owner: {
      aiAnchorText: ownerAiAnchor,
      aiInstructionText: ownerAiInstruction,
      aiText: ownerAi,
      commentAnchorText: ownerCommentAnchor,
      commentText: ownerComment,
    },
  }
}

function parseSelectedTransports(
  raw: RawExecution | undefined,
  fixturePath: string,
  environment: Environment,
  reasons: string[],
): CollaborationTransport[] {
  if (raw === undefined) return [...COLLABORATION_TRANSPORTS]
  const executionReasons: string[] = []
  const contract = requiredString(
    raw.contract,
    'fixture.execution.contract',
    executionReasons,
  )
  const runId = requiredString(
    raw.runId,
    'fixture.execution.runId',
    executionReasons,
  )
  const transport = requiredString(
    raw.transport,
    'fixture.execution.transport',
    executionReasons,
  )
  const generatedProfile = contract === GENERATED_SYSTEM_SMOKE_CONTRACT
    ? 'system-smoke'
    : contract === GENERATED_FAULT_INJECTION_CONTRACT
      ? 'fault-injection'
      : null
  if (contract && generatedProfile === null) {
    executionReasons.push(
      'fixture.execution.contract does not identify a supported generated fixture',
    )
  }
  if (
    transport
    && transport !== GENERATED_SYSTEM_SMOKE_TRANSPORT
    && transport !== GENERATED_FAULT_INJECTION_TRANSPORT
  ) {
    executionReasons.push(
      `fixture.execution.transport must equal "${GENERATED_SYSTEM_SMOKE_TRANSPORT}"`,
    )
  }
  if (
    runId
    && runId !== nonEmptyString(environment.INQTRIX_VERIFICATION_RUN_ID)
  ) {
    executionReasons.push(
      'fixture.execution.runId must equal INQTRIX_VERIFICATION_RUN_ID',
    )
  }
  if (
    generatedProfile
    && environment.INQTRIX_VERIFICATION_PROFILE !== generatedProfile
  ) {
    executionReasons.push(
      `fixture.execution requires the ${generatedProfile} verification profile`,
    )
  }
  const reportDirectory = nonEmptyString(
    environment.INQTRIX_VERIFICATION_REPORT_DIR,
  )
  if (
    !reportDirectory
    || !fixtureIsInsidePrivateRunDirectory(fixturePath, reportDirectory)
  ) {
    executionReasons.push(
      'fixture.execution requires a fixture inside the private run directory',
    )
  }
  if (!privateCredentialFile(fixturePath)) {
    executionReasons.push(
      'generated collaboration fixture must not be accessible by group or other users',
    )
  }
  reasons.push(...executionReasons)
  return executionReasons.length === 0
    ? [transport as CollaborationTransport]
    : [...COLLABORATION_TRANSPORTS]
}

function parseSuggestionDocument(
  raw: RawSuggestionDocument | undefined,
  reasons: string[],
): CollaborationE2EStack['documents']['suggestion'] | null {
  if (!isRecord(raw)) {
    reasons.push('fixture.documents.suggestion must be an object')
    return null
  }
  const documentId = requiredString(
    raw.documentId,
    'fixture.documents.suggestion.documentId',
    reasons,
  )
  const expectedAuthorId = requiredString(
    raw.expectedAuthorId,
    'fixture.documents.suggestion.expectedAuthorId',
    reasons,
  )
  const expectedPermission = requiredString(
    raw.expectedPermission,
    'fixture.documents.suggestion.expectedPermission',
    reasons,
  )
  if (expectedPermission && expectedPermission !== 'suggest') {
    reasons.push('fixture.documents.suggestion.expectedPermission must equal "suggest"')
  }
  if (expectedAuthorId && !isUuid(expectedAuthorId)) {
    reasons.push('fixture.documents.suggestion.expectedAuthorId must be a UUID')
  }
  if (
    !documentId
    || !expectedAuthorId
    || expectedPermission !== 'suggest'
    || !isUuid(expectedAuthorId)
  ) return null
  return { documentId, expectedAuthorId, expectedPermission }
}

function parseControls(
  raw: RawControls | undefined,
  reasons: string[],
  environment: Environment,
  requireRunScope: boolean,
): CollaborationControlFixture | null {
  if (raw === undefined) return null
  const baseURL = requiredString(raw.baseURL, 'fixture.controls.baseURL', reasons)
  const authorizationEnv = requiredString(
    raw.authorizationEnv,
    'fixture.controls.authorizationEnv',
    reasons,
  )
  const runId = optionalCapabilityString(raw.runId)
  const paths = {
    armGatewayOutage: requiredControlPath(
      raw.armGatewayOutagePath,
      'fixture.controls.armGatewayOutagePath',
      reasons,
    ),
    armLostAck: requiredControlPath(
      raw.armLostAckPath,
      'fixture.controls.armLostAckPath',
      reasons,
    ),
    armOutage: requiredControlPath(
      raw.armOutagePath,
      'fixture.controls.armOutagePath',
      reasons,
    ),
    operationStatus: requiredControlPath(
      raw.operationStatusPath,
      'fixture.controls.operationStatusPath',
      reasons,
    ),
    restart: requiredControlPath(raw.restartPath, 'fixture.controls.restartPath', reasons),
    restore: requiredControlPath(raw.restorePath, 'fixture.controls.restorePath', reasons),
  }
  if (baseURL && (!isHttpUrl(baseURL) || hasUrlSecrets(baseURL))) {
    reasons.push('fixture.controls.baseURL must be an HTTP(S) URL without credentials, query, or fragment')
  }
  if (authorizationEnv && !/^[A-Z][A-Z0-9_]*$/.test(authorizationEnv)) {
    reasons.push('fixture.controls.authorizationEnv must name an uppercase environment variable')
  }
  if (requireRunScope && !runId) {
    reasons.push('fixture.controls.runId is required for generated fault control')
  }
  if (
    runId
    && runId !== nonEmptyString(environment.INQTRIX_VERIFICATION_RUN_ID)
  ) {
    reasons.push('fixture.controls.runId must equal INQTRIX_VERIFICATION_RUN_ID')
  }
  if (!baseURL || !authorizationEnv || Object.values(paths).some((value) => !value) || reasons.length > 0) {
    return null
  }
  return {
    authorizationEnv,
    baseURL: normalizeHttpUrl(baseURL),
    paths,
    runId,
  }
}

function parseUser(
  raw: RawUser | undefined,
  field: string,
  fixturePath: string,
  reasons: string[],
): { displayName: string; storageState: string; userId: string } | null {
  const displayName = requiredString(raw?.displayName, `${field}.displayName`, reasons)
  const storageState = requiredString(raw?.storageState, `${field}.storageState`, reasons)
  const userId = requiredString(raw?.userId, `${field}.userId`, reasons)
  if (userId && !isUuid(userId)) {
    reasons.push(`${field}.userId must be a UUID`)
  }
  if (!displayName || !storageState || !userId || !isUuid(userId)) return null
  return {
    displayName,
    storageState: resolveOptionalPath(storageState, dirname(fixturePath))!,
    userId,
  }
}

function requiredControlPath(value: unknown, field: string, reasons: string[]): string {
  const path = requiredString(value, field, reasons)
  if (!path) return ''
  if (!path.startsWith('/') || path.startsWith('//') || path.includes('?') || path.includes('#')) {
    reasons.push(`${field} must be an absolute path without query or fragment`)
    return ''
  }
  return path
}

function requiredString(value: unknown, field: string, reasons: string[]): string {
  const parsed = nonEmptyString(value)
  if (!parsed) reasons.push(`${field} must be a non-empty string`)
  return parsed ?? ''
}

function optionalCapabilityString(value: unknown): string | null {
  return nonEmptyString(value)
}

function nonEmptyString(value: unknown): string | null {
  return typeof value === 'string' && value.trim() ? value.trim() : null
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value)
}

function isUuid(value: string): boolean {
  return /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value)
}

function resolveOptionalPath(value: unknown, baseDirectory: string): string | null {
  const parsed = nonEmptyString(value)
  if (!parsed) return null
  return isAbsolute(parsed) ? parsed : resolve(baseDirectory, parsed)
}

function normalizeHttpUrl(value: string): string {
  const url = new URL(value)
  if (!url.pathname.endsWith('/')) url.pathname = `${url.pathname}/`
  return url.toString()
}

function isHttpUrl(value: string): boolean {
  try {
    return ['http:', 'https:'].includes(new URL(value).protocol)
  } catch {
    return false
  }
}

function hasUrlSecrets(value: string): boolean {
  try {
    const url = new URL(value)
    return Boolean(url.username || url.password || url.search || url.hash)
  } catch {
    return true
  }
}

function privateCredentialFile(path: string): boolean {
  if (process.platform === 'win32') return true
  try {
    const metadata = statSync(path)
    return metadata.isFile() && (metadata.mode & 0o077) === 0
  } catch {
    return false
  }
}

function uniqueStrings(values: string[]): string[] {
  return [...new Set(values)]
}

export const collaborationE2EConfiguration = loadCollaborationE2EConfiguration()
if (collaborationE2EConfiguration.mode === 'strict') {
  const profile = process.env.INQTRIX_VERIFICATION_PROFILE
  if (profile !== 'system-smoke' && profile !== 'fault-injection') {
    throw new Error(
      'INQTRIX_VERIFICATION_PROFILE must be "system-smoke" or "fault-injection" in strict mode.',
    )
  }
  assertStrictE2EConfiguration(
    collaborationE2EConfiguration,
    profile,
  )
}
