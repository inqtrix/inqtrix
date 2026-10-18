export const GENERATED_FAULT_INJECTION_CONTRACT:
  'inqtrix-generated-fault-injection-v1'
export const GENERATED_FAULT_INJECTION_TRANSPORT: 'python-gateway'
export const GENERATED_FAULT_INJECTION_PRIVATE_ANCHOR_TARGETS: readonly [
  'chromium-desktop',
  'chromium-mobile',
  'firefox-desktop',
  'webkit-desktop',
]

export type GeneratedFaultInjectionPrivateAnchorTarget =
  typeof GENERATED_FAULT_INJECTION_PRIVATE_ANCHOR_TARGETS[number]

export type GeneratedFaultInjectionActor = {
  displayName: string
  storageState: string
  userId: string
}

export type GeneratedFaultInjectionPrivateAnchor = {
  aiAnchorText: string
  aiInstructionText: string
  aiText: string
  commentAnchorText: string
  commentText: string
}

export type GeneratedFaultInjectionDocuments = {
  directEdit: string | { id: string }
  downgrade: string | { id: string }
  gatewayOutage: string | { id: string }
  outage: string | { id: string }
  protocol: string | { id: string }
  reconciliation: string | { id: string }
  revocation: string | { id: string }
  suggestion: string | { id: string }
}

export function buildGeneratedFaultInjectionFixture(options: {
  baseURL?: string
  collaborator: GeneratedFaultInjectionActor
  controls: {
    authorizationEnv: string
    baseURL: string
    paths: Record<
      | 'armGatewayOutage'
      | 'armLostAck'
      | 'armOutage'
      | 'operationStatus'
      | 'restart'
      | 'restore',
      string
    >
  }
  documents: GeneratedFaultInjectionDocuments
  owner: GeneratedFaultInjectionActor
  privateAnchors: {
    collaborator: GeneratedFaultInjectionPrivateAnchor
    documents: Record<
      GeneratedFaultInjectionPrivateAnchorTarget,
      string | { id: string }
    >
    owner: GeneratedFaultInjectionPrivateAnchor
  }
  runId: string
}): unknown

export function writeGeneratedFaultInjectionFixture(
  path: string,
  fixture: unknown,
): Promise<void>

export function fixtureIsInsidePrivateRunDirectory(
  fixturePath: string,
  reportDirectory: string,
): boolean
