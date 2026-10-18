import type { CollaborationControlFixture } from './config.ts'

export type FixtureOperationState = {
  closeCode: number | null
  durabilityReconciled: boolean | null
  durableSequence: number | null
  operationId: string
  outageLayer: 'collaboration_sidecar' | 'fastapi_gateway' | null
  pendingDurabilityCount: number | null
  projectionSequence: number | null
  reconciliationSequence: number | null
  state: 'armed' | 'failed' | 'outage' | 'ready' | 'triggered'
}

export type ControlPath =
  | 'armGatewayOutage'
  | 'armLostAck'
  | 'armOutage'
  | 'operationStatus'
  | 'restart'
  | 'restore'
type FetchImplementation = typeof fetch

const CONTROL_REQUEST_TIMEOUT_MS = 5_000
const CONTROL_RECOVERY_TIMEOUT_MS = 45_000

export function controlRequestTimeoutMs(path: ControlPath): number {
  return path === 'restart' || path === 'restore'
    ? CONTROL_RECOVERY_TIMEOUT_MS
    : CONTROL_REQUEST_TIMEOUT_MS
}

export class CollaborationFixtureControlClient {
  private readonly authorization: string
  private readonly fetchImplementation: FetchImplementation
  private readonly fixture: CollaborationControlFixture

  constructor(
    fixture: CollaborationControlFixture,
    environment: Record<string, string | undefined> = process.env,
    fetchImplementation: FetchImplementation = fetch,
  ) {
    const token = environment[fixture.authorizationEnv]?.trim()
    if (!token) {
      throw new Error(
        `${fixture.authorizationEnv} is required for collaboration fixture control authorization.`,
      )
    }
    this.authorization = `Bearer ${token}`
    this.fetchImplementation = fetchImplementation
    this.fixture = fixture
  }

  async armLostAck(documentId: string, userId: string): Promise<FixtureOperationState> {
    return this.invoke('armLostAck', { document_id: documentId, user_id: userId })
  }

  async armGatewayOutage(documentId: string, userId: string): Promise<FixtureOperationState> {
    return this.invoke('armGatewayOutage', { document_id: documentId, user_id: userId })
  }

  async armOutage(documentId: string, userId: string): Promise<FixtureOperationState> {
    return this.invoke('armOutage', { document_id: documentId, user_id: userId })
  }

  async status(operationId: string): Promise<FixtureOperationState> {
    return this.invoke('operationStatus', { operation_id: operationId })
  }

  async restore(operationId: string): Promise<FixtureOperationState> {
    return this.invoke('restore', { operation_id: operationId })
  }

  async restart(documentId: string): Promise<FixtureOperationState> {
    return this.invoke('restart', { document_id: documentId })
  }

  async waitForState(
    operationId: string,
    expectedState: FixtureOperationState['state'],
    timeoutMs = 30_000,
  ): Promise<FixtureOperationState> {
    const deadline = Date.now() + timeoutMs
    let lastState: FixtureOperationState['state'] | null = null
    while (Date.now() < deadline) {
      const status = await this.status(operationId)
      lastState = status.state
      if (status.state === expectedState) return status
      if (status.state === 'failed') {
        throw new Error(`Fixture operation ${operationId} entered failed state.`)
      }
      await new Promise((resolve) => setTimeout(resolve, 100))
    }
    throw new Error(
      `Fixture operation ${operationId} did not reach ${expectedState}; last state was ${lastState ?? 'unknown'}.`,
    )
  }

  async waitForDurabilityReconciliation(
    operationId: string,
    timeoutMs = 30_000,
  ): Promise<FixtureOperationState> {
    const deadline = Date.now() + timeoutMs
    let last: FixtureOperationState | null = null
    while (Date.now() < deadline) {
      last = await this.status(operationId)
      if (last.state === 'failed') {
        throw new Error(`Fixture operation ${operationId} entered failed state.`)
      }
      if (
        last.state === 'ready'
        && last.durabilityReconciled === true
        && last.pendingDurabilityCount === 0
        && last.reconciliationSequence !== null
      ) return last
      await new Promise((resolve) => setTimeout(resolve, 100))
    }
    throw new Error(
      `Fixture operation ${operationId} did not report reconciled durability with zero pending updates; last state was ${last?.state ?? 'unknown'}.`,
    )
  }

  private async invoke(
    path: ControlPath,
    body: Record<string, string>,
  ): Promise<FixtureOperationState> {
    let response: Response
    const headers: Record<string, string> = {
      Accept: 'application/json',
      Authorization: this.authorization,
      'Content-Type': 'application/json',
    }
    if (this.fixture.runId) {
      headers['X-Inqtrix-Verification-Run-Id'] = this.fixture.runId
    }
    try {
      response = await this.fetchImplementation(
        new URL(this.fixture.paths[path], this.fixture.baseURL),
        {
          body: JSON.stringify(body),
          headers,
          method: 'POST',
          redirect: 'error',
          signal: AbortSignal.timeout(controlRequestTimeoutMs(path)),
        },
      )
    } catch {
      throw new Error(`Collaboration fixture control ${path} failed before receiving a response.`)
    }
    if (!response.ok) {
      await response.body?.cancel().catch(() => {})
      throw new Error(`Collaboration fixture control ${path} returned HTTP ${response.status}.`)
    }

    let payload: unknown
    try {
      payload = await response.json()
    } catch {
      throw new Error(`Collaboration fixture control ${path} returned invalid JSON.`)
    }
    return parseOperationState(payload, path)
  }
}

export function parseOperationState(
  value: unknown,
  operation = 'operationStatus',
): FixtureOperationState {
  if (!isRecord(value)) {
    throw new Error(`Collaboration fixture control ${operation} returned a non-object payload.`)
  }
  const operationId = requiredString(value.operation_id, `${operation}.operation_id`)
  const state = value.state
  if (!['armed', 'failed', 'outage', 'ready', 'triggered'].includes(String(state))) {
    throw new Error(`Collaboration fixture control ${operation}.state is invalid.`)
  }
  return {
    closeCode: optionalSafeInteger(value.close_code, `${operation}.close_code`),
    durabilityReconciled: optionalBoolean(
      value.durability_reconciled,
      `${operation}.durability_reconciled`,
    ),
    durableSequence: optionalSafeInteger(
      value.durable_sequence,
      `${operation}.durable_sequence`,
    ),
    operationId,
    outageLayer: optionalOutageLayer(value.outage_layer, `${operation}.outage_layer`),
    pendingDurabilityCount: optionalSafeInteger(
      value.pending_durability_count,
      `${operation}.pending_durability_count`,
    ),
    projectionSequence: optionalSafeInteger(
      value.projection_sequence,
      `${operation}.projection_sequence`,
    ),
    reconciliationSequence: optionalSafeInteger(
      value.reconciliation_sequence,
      `${operation}.reconciliation_sequence`,
    ),
    state: state as FixtureOperationState['state'],
  }
}

function optionalOutageLayer(
  value: unknown,
  field: string,
): FixtureOperationState['outageLayer'] {
  if (value === undefined || value === null) return null
  if (value !== 'collaboration_sidecar' && value !== 'fastapi_gateway') {
    throw new Error(`${field} must identify collaboration_sidecar or fastapi_gateway when supplied.`)
  }
  return value
}

function optionalBoolean(value: unknown, field: string): boolean | null {
  if (value === undefined || value === null) return null
  if (typeof value !== 'boolean') throw new Error(`${field} must be a boolean when supplied.`)
  return value
}

function optionalSafeInteger(value: unknown, field: string): number | null {
  if (value === undefined || value === null) return null
  if (!Number.isSafeInteger(value) || Number(value) < 0) {
    throw new Error(`${field} must be a non-negative safe integer when supplied.`)
  }
  return Number(value)
}

function requiredString(value: unknown, field: string): string {
  if (typeof value !== 'string' || !value.trim()) {
    throw new Error(`${field} must be a non-empty string.`)
  }
  return value.trim()
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value)
}
