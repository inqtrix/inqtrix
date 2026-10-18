export const GENERATED_SYSTEM_SMOKE_CONTRACT:
  'inqtrix-generated-system-smoke-v1'
export const GENERATED_SYSTEM_SMOKE_TRANSPORT: 'python-gateway'

export type GeneratedSystemSmokeActor = {
  displayName: string
  storageState: string
  userId: string
}

export type GeneratedSystemSmokeDocuments = {
  concurrent: string | { id: string }
  detachedTransfer: string | { id: string }
  directEdit: string | { id: string }
  ime: string | { id: string }
  largeState: string | { id: string }
  layout: string | { id: string }
  mobileDrawers: string | { id: string }
  remotePresence: string | { id: string }
  revocation: string | { id: string }
  sourceReadonly: string | { id: string }
  staysConnected: string | { id: string }
  aiSuggestion: string | { id: string }
  suggestion: string | { id: string }
  suggestionUndo: string | { id: string }
}

export function normalizeSystemSmokeBaseURL(value?: string): string

export function buildGeneratedSystemSmokeFixture(options: {
  baseURL?: string
  collaborator: GeneratedSystemSmokeActor
  documents: GeneratedSystemSmokeDocuments
  owner: GeneratedSystemSmokeActor
  runId: string
}): {
  documents: {
    concurrent: string
    detachedTransfer: string
    directEdit: string
    ime: string
    largeState: string
    layout: string
    mobileDrawers: string
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
  execution: {
    contract: typeof GENERATED_SYSTEM_SMOKE_CONTRACT
    runId: string
    transport: typeof GENERATED_SYSTEM_SMOKE_TRANSPORT
  }
  locale: 'de'
  transports: {
    'python-gateway': { baseURL: string }
  }
  users: {
    collaborator: GeneratedSystemSmokeActor
    owner: GeneratedSystemSmokeActor
  }
  version: 2
}

export function writeGeneratedSystemSmokeFixture(
  path: string,
  fixture: unknown,
): Promise<void>

export function fixtureIsInsidePrivateRunDirectory(
  fixturePath: string,
  reportDirectory: string,
): boolean
