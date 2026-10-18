import { randomUUID, timingSafeEqual } from 'node:crypto'
import { createServer } from 'node:http'
import { resolve } from 'node:path'
import { setTimeout as delay } from 'node:timers/promises'

import { requireContainerControlCommand } from './container-control.mjs'
import { assertVerificationRunId } from './run-scope.mjs'

const FAULT_FILE = '/tmp/inqtrix-collaboration-verification-fault.json'
const FAULT_CONTRACT = 'inqtrix-collaboration-verification-fault-v1'
const MAX_BODY_BYTES = 8 * 1024
const LOAD_TIMEOUT_MS = 5_000
const SERVICE_READY_TIMEOUT_MS = 30_000
const COMPOSE_SERVICE = 'com.docker.compose.service'
const COMPOSE_PROJECT = 'com.docker.compose.project'
const COMPOSE_FILES = 'com.docker.compose.project.config_files'

const ROUTES = Object.freeze({
  armGatewayOutage: '/faults/gateway-outage/arm',
  armLostAck: '/faults/lost-ack/arm',
  armOutage: '/faults/sidecar-outage/arm',
  operationStatus: '/faults/operation/status',
  restart: '/faults/restart',
  restore: '/faults/restore',
})

export async function resolveFaultControlContainers({
  engine,
  repositoryRoot,
}) {
  const rows = await listContainers(engine, repositoryRoot)
  const canonicalCompose = resolve(
    repositoryRoot,
    'deploy/compose/compose.stack.yaml',
  )
  const candidates = rows.filter((row) => {
    const labels = normalizedLabels(row)
    return String(labels[COMPOSE_FILES] ?? '')
      .split(',')
      .map((value) => resolve(value.trim()))
      .includes(canonicalCompose)
  })
  const web = oneService(candidates, 'web')
  const collaboration = oneService(candidates, 'collaboration')
  const webLabels = normalizedLabels(web)
  const collaborationLabels = normalizedLabels(collaboration)
  if (
    !webLabels[COMPOSE_PROJECT]
    || webLabels[COMPOSE_PROJECT] !== collaborationLabels[COMPOSE_PROJECT]
  ) {
    throw new Error('Fault-control targets do not belong to one Compose project.')
  }
  if (!isRunning(web) || !isRunning(collaboration)) {
    throw new Error('Fault-control requires running web and collaboration services.')
  }
  return {
    collaboration: requiredContainerId(collaboration),
    project: webLabels[COMPOSE_PROJECT],
    web: requiredContainerId(web),
  }
}

export class ContainerFaultDriver {
  #collaboration
  #engine
  #repositoryRoot
  #web
  #webStopped = false

  constructor({ collaboration, engine, repositoryRoot, web }) {
    this.#collaboration = collaboration
    this.#engine = engine
    this.#repositoryRoot = repositoryRoot
    this.#web = web
  }

  async initialize() {
    await Promise.all([
      this.#waitForHealthy(this.#collaboration, 'collaboration'),
      this.#waitForHealthy(this.#web, 'web'),
    ])
    await requireContainerControlCommand(
      this.#engine,
      [
        'exec',
        this.#collaboration,
        'node',
        '-e',
        "if(process.env.INQTRIX_COLLABORATION_VERIFICATION_FAULTS!=='1')process.exit(23)",
      ],
      this.#repositoryRoot,
      'Checking the explicit collaboration verification-fault capability',
    )
    await this.#removeFaultFile()
  }

  async arm(record) {
    await this.#writeFaultFile(record)
    await this.#signalFaultReload()
    return await this.#waitForLoaded(record.operation_id)
  }

  async read(operationId) {
    const record = await this.#readFaultFile()
    if (record.operation_id !== operationId) {
      throw new Error('The collaboration fault operation does not match the active record.')
    }
    return record
  }

  async restore(operationId) {
    const current = await this.read(operationId)
    await this.#writeFaultFile({
      ...current,
      loaded: false,
      state: 'ready',
    })
    await this.#signalFaultReload()
    return await this.#waitForLoaded(operationId)
  }

  async stopGateway() {
    await requireContainerControlCommand(
      this.#engine,
      ['stop', '--time', '1', this.#web],
      this.#repositoryRoot,
      'Stopping the selected web gateway',
    )
    this.#webStopped = true
  }

  async startGateway() {
    await requireContainerControlCommand(
      this.#engine,
      ['start', this.#web],
      this.#repositoryRoot,
      'Starting the selected web gateway',
    )
    await this.#waitForHealthy(this.#web, 'web')
    this.#webStopped = false
  }

  async restartSidecar() {
    await requireContainerControlCommand(
      this.#engine,
      ['restart', this.#collaboration],
      this.#repositoryRoot,
      'Restarting the selected collaboration sidecar',
    )
    await this.#waitForHealthy(this.#collaboration, 'collaboration')
  }

  async cleanup() {
    if (this.#webStopped) await this.startGateway()
    try {
      const current = await this.#readFaultFile()
      if (current.state !== 'ready') {
        await this.#writeFaultFile({ ...current, loaded: false, state: 'ready' })
        await this.#signalFaultReload()
        await this.#waitForLoaded(current.operation_id)
      }
    } catch {
      // A missing record is the normal state before the first sidecar fault.
    }
    await this.#removeFaultFile()
  }

  async #waitForLoaded(operationId) {
    const deadline = Date.now() + LOAD_TIMEOUT_MS
    let current = null
    while (Date.now() < deadline) {
      current = await this.#readFaultFile().catch(() => null)
      if (current?.operation_id === operationId && current.loaded === true) return current
      await delay(25)
    }
    throw new Error('The collaboration sidecar did not load the fault operation.')
  }

  async #waitForHealthy(container, service) {
    const deadline = Date.now() + SERVICE_READY_TIMEOUT_MS
    let latest = null
    while (Date.now() < deadline) {
      const result = await requireContainerControlCommand(
        this.#engine,
        ['inspect', '--format', '{{json .State}}', container],
        this.#repositoryRoot,
        `Inspecting the selected ${service} container state`,
      )
      latest = parseContainerRuntimeState(result.stdout)
      if (latest.status === 'running' && latest.health === 'healthy') return
      if (['dead', 'exited', 'removing'].includes(latest.status)) {
        throw new Error(`The selected ${service} container entered ${latest.status}.`)
      }
      await delay(100)
    }
    throw new Error(
      `The selected ${service} container did not become healthy (last state: ${
        latest ? `${latest.status}/${latest.health}` : 'unavailable'
      }).`,
    )
  }

  async #signalFaultReload() {
    await requireContainerControlCommand(
      this.#engine,
      ['kill', '--signal', 'SIGUSR2', this.#collaboration],
      this.#repositoryRoot,
      'Signalling the selected collaboration sidecar',
    )
  }

  async #writeFaultFile(record) {
    const script = [
      "const fs=require('node:fs')",
      `const target=${JSON.stringify(FAULT_FILE)}`,
      "const temporary=target+'.control.tmp'",
      "let body=''",
      "process.stdin.setEncoding('utf8')",
      "process.stdin.on('data',(chunk)=>{body+=chunk})",
      "process.stdin.on('end',()=>{fs.writeFileSync(temporary,body,{mode:0o600});fs.renameSync(temporary,target)})",
    ].join(';')
    await requireContainerControlCommand(
      this.#engine,
      ['exec', '--interactive', this.#collaboration, 'node', '-e', script],
      this.#repositoryRoot,
      'Writing the private collaboration fault record',
      `${JSON.stringify(record)}\n`,
    )
  }

  async #readFaultFile() {
    const script = [
      "const fs=require('node:fs')",
      `process.stdout.write(fs.readFileSync(${JSON.stringify(FAULT_FILE)},'utf8'))`,
    ].join(';')
    const result = await requireContainerControlCommand(
      this.#engine,
      ['exec', this.#collaboration, 'node', '-e', script],
      this.#repositoryRoot,
      'Reading the private collaboration fault record',
    )
    return parseFaultRecord(JSON.parse(result.stdout))
  }

  async #removeFaultFile() {
    const script = [
      "const fs=require('node:fs')",
      `try{fs.unlinkSync(${JSON.stringify(FAULT_FILE)})}catch(error){if(error.code!=='ENOENT')throw error}`,
    ].join(';')
    await requireContainerControlCommand(
      this.#engine,
      ['exec', this.#collaboration, 'node', '-e', script],
      this.#repositoryRoot,
      'Removing the private collaboration fault record',
    )
  }
}

export async function startFaultControlServer({
  allowedDocuments,
  driver,
  runId,
  token,
}) {
  assertVerificationRunId(runId)
  if (typeof token !== 'string' || Buffer.byteLength(token, 'utf8') < 32) {
    throw new Error('Fault-control authorization must contain at least 32 UTF-8 bytes.')
  }
  const allowed = normalizeAllowedDocuments(allowedDocuments)
  const operations = new Map()
  let activeSidecarOperation = null

  const server = createServer(async (request, response) => {
    const fail = (status, reason) => json(response, status, { error: { reason } })
    try {
      if (!isLoopback(request.socket.remoteAddress)) return fail(403, 'loopback_required')
      if (request.method !== 'POST') return fail(405, 'method_not_allowed')
      if (!authorized(request.headers.authorization, token)) return fail(401, 'unauthorized')
      if (request.headers['x-inqtrix-verification-run-id'] !== runId) {
        return fail(409, 'run_scope_mismatch')
      }
      if (request.headers['content-type']?.split(';', 1)[0]?.trim() !== 'application/json') {
        return fail(415, 'content_type_required')
      }
      const body = await readJson(request)
      const path = new URL(request.url ?? '/', 'http://127.0.0.1').pathname
      if (path === ROUTES.armLostAck) {
        const target = requireTarget(body, allowed)
        if (activeSidecarOperation) return fail(409, 'sidecar_fault_active')
        const operation = faultRecord(runId, target, 'lost_ack')
        activeSidecarOperation = operation.operation_id
        operations.set(operation.operation_id, { kind: 'lost_ack' })
        const loaded = await driver.arm(operation)
        return json(response, 200, publicState(loaded))
      }
      if (path === ROUTES.armOutage) {
        const target = requireTarget(body, allowed)
        if (activeSidecarOperation) return fail(409, 'sidecar_fault_active')
        const operation = faultRecord(runId, target, 'sidecar_outage')
        activeSidecarOperation = operation.operation_id
        operations.set(operation.operation_id, { kind: 'sidecar_outage' })
        const loaded = await driver.arm(operation)
        return json(response, 200, publicState(loaded))
      }
      if (path === ROUTES.armGatewayOutage) {
        const target = requireTarget(body, allowed)
        const operationId = randomUUID()
        const operation = {
          operation_id: operationId,
          outage_layer: 'fastapi_gateway',
          state: 'armed',
        }
        const tracked = {
          kind: 'gateway_outage',
          operation,
          task: null,
        }
        operations.set(operationId, tracked)
        tracked.task = driver.stopGateway().then(() => {
          tracked.operation = { ...operation, state: 'outage' }
        }).catch(() => {
          tracked.operation = { ...operation, state: 'failed' }
        })
        return json(response, 200, operation)
      }
      if (path === ROUTES.operationStatus) {
        const operationId = requireOperationId(body)
        const tracked = operations.get(operationId)
        if (!tracked) return fail(404, 'operation_not_found')
        if (tracked.kind === 'gateway_outage' || tracked.kind === 'restart') {
          return json(response, 200, tracked.operation)
        }
        return json(response, 200, publicState(await driver.read(operationId)))
      }
      if (path === ROUTES.restore) {
        const operationId = requireOperationId(body)
        const tracked = operations.get(operationId)
        if (!tracked) return fail(404, 'operation_not_found')
        if (tracked.kind === 'gateway_outage') {
          await tracked.task
          if (tracked.operation.state === 'failed') return fail(500, 'gateway_stop_failed')
          await driver.startGateway()
          tracked.operation = { ...tracked.operation, state: 'ready' }
          return json(response, 200, tracked.operation)
        }
        if (tracked.kind === 'restart') return json(response, 200, tracked.operation)
        const restored = await driver.restore(operationId)
        activeSidecarOperation = null
        return json(response, 200, publicState(restored))
      }
      if (path === ROUTES.restart) {
        requireDocument(body, allowed)
        const operationId = randomUUID()
        await driver.restartSidecar()
        const operation = {
          operation_id: operationId,
          outage_layer: 'collaboration_sidecar',
          state: 'ready',
        }
        operations.set(operationId, { kind: 'restart', operation })
        activeSidecarOperation = null
        return json(response, 200, operation)
      }
      return fail(404, 'not_found')
    } catch (error) {
      const status = error instanceof ControlRequestError ? error.status : 500
      return fail(status, status === 500 ? 'control_failed' : error.reason)
    }
  })

  await new Promise((resolvePromise, reject) => {
    server.once('error', reject)
    server.listen(0, '127.0.0.1', resolvePromise)
  })
  const address = server.address()
  if (!address || typeof address === 'string') {
    server.close()
    throw new Error('Fault-control did not bind a loopback TCP address.')
  }
  return {
    baseURL: `http://127.0.0.1:${address.port}`,
    paths: ROUTES,
    async close() {
      await new Promise((resolvePromise, reject) => {
        server.close((error) => error ? reject(error) : resolvePromise())
      })
      await driver.cleanup()
    },
  }
}

export function publicState(record) {
  return {
    close_code: record.close_code ?? null,
    durability_reconciled: record.durability_reconciled ?? null,
    durable_sequence: record.durable_sequence ?? null,
    operation_id: record.operation_id,
    outage_layer: record.kind === 'sidecar_outage'
      ? 'collaboration_sidecar'
      : null,
    pending_durability_count: record.pending_durability_count ?? null,
    projection_sequence: record.projection_sequence ?? null,
    reconciliation_sequence: record.reconciliation_sequence ?? null,
    state: record.state,
  }
}

export function parseContainerRuntimeState(value) {
  let parsed
  try {
    parsed = JSON.parse(String(value).trim())
  } catch {
    throw new Error('The container engine returned an invalid runtime state.')
  }
  const status = parsed?.Status
  const health = parsed?.Health?.Status
  if (typeof status !== 'string' || typeof health !== 'string') {
    throw new Error('The selected container does not expose a health state.')
  }
  return {
    health: health.toLowerCase(),
    status: status.toLowerCase(),
  }
}

function faultRecord(runId, target, kind) {
  return {
    close_code: null,
    contract: FAULT_CONTRACT,
    document_id: target.documentId,
    durability_reconciled: null,
    durable_sequence: null,
    kind,
    loaded: false,
    operation_id: randomUUID(),
    pending_durability_count: null,
    projection_sequence: null,
    reconciliation_sequence: null,
    run_id: runId,
    state: 'armed',
    update_hash: null,
    user_id: target.userId,
  }
}

function parseFaultRecord(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error('The collaboration sidecar returned an invalid fault record.')
  }
  if (
    value.contract !== FAULT_CONTRACT
    || typeof value.operation_id !== 'string'
    || typeof value.run_id !== 'string'
    || typeof value.document_id !== 'string'
    || typeof value.user_id !== 'string'
    || !['lost_ack', 'sidecar_outage'].includes(value.kind)
    || !['armed', 'failed', 'outage', 'ready', 'triggered'].includes(value.state)
    || typeof value.loaded !== 'boolean'
  ) {
    throw new Error('The collaboration sidecar returned an invalid fault record.')
  }
  return value
}

function requireTarget(body, allowed) {
  if (!body || typeof body !== 'object' || Array.isArray(body)) {
    throw new ControlRequestError(400, 'invalid_request')
  }
  const keys = Object.keys(body).sort().join(',')
  if (keys !== 'document_id,user_id') {
    throw new ControlRequestError(400, 'invalid_request')
  }
  const users = allowed.get(body.document_id)
  if (!users?.has(body.user_id)) {
    throw new ControlRequestError(404, 'target_not_found')
  }
  return { documentId: body.document_id, userId: body.user_id }
}

function requireDocument(body, allowed) {
  if (
    !body
    || typeof body !== 'object'
    || Array.isArray(body)
    || Object.keys(body).join(',') !== 'document_id'
    || !allowed.has(body.document_id)
  ) throw new ControlRequestError(404, 'target_not_found')
  return body.document_id
}

function requireOperationId(body) {
  if (
    !body
    || typeof body !== 'object'
    || Array.isArray(body)
    || Object.keys(body).join(',') !== 'operation_id'
    || typeof body.operation_id !== 'string'
  ) throw new ControlRequestError(400, 'invalid_request')
  return body.operation_id
}

function normalizeAllowedDocuments(value) {
  const allowed = new Map()
  for (const [documentId, userIds] of Object.entries(value ?? {})) {
    if (typeof documentId !== 'string' || !documentId || !Array.isArray(userIds)) {
      throw new Error('Fault-control targets are invalid.')
    }
    allowed.set(documentId, new Set(userIds))
  }
  if (allowed.size === 0) throw new Error('Fault-control requires at least one target document.')
  return allowed
}

async function readJson(request) {
  const chunks = []
  let size = 0
  for await (const chunk of request) {
    size += chunk.length
    if (size > MAX_BODY_BYTES) throw new ControlRequestError(413, 'body_too_large')
    chunks.push(chunk)
  }
  try {
    return JSON.parse(Buffer.concat(chunks).toString('utf8'))
  } catch {
    throw new ControlRequestError(400, 'invalid_json')
  }
}

function authorized(header, expected) {
  const actual = typeof header === 'string' && header.startsWith('Bearer ')
    ? header.slice('Bearer '.length)
    : ''
  const actualBytes = Buffer.from(actual)
  const expectedBytes = Buffer.from(expected)
  return actualBytes.length === expectedBytes.length
    && timingSafeEqual(actualBytes, expectedBytes)
}

function isLoopback(value) {
  return value === '127.0.0.1' || value === '::1' || value === '::ffff:127.0.0.1'
}

function json(response, status, payload) {
  response.writeHead(status, {
    'Cache-Control': 'no-store',
    'Content-Type': 'application/json; charset=utf-8',
  })
  response.end(`${JSON.stringify(payload)}\n`)
}

class ControlRequestError extends Error {
  constructor(status, reason) {
    super(reason)
    this.status = status
    this.reason = reason
  }
}

async function listContainers(engine, cwd) {
  if (engine === 'podman') {
    const result = await requireContainerControlCommand(
      engine,
      ['ps', '--all', '--format', 'json'],
      cwd,
      'Listing Compose containers',
    )
    const value = JSON.parse(result.stdout)
    if (!Array.isArray(value)) throw new Error('Podman returned an invalid container inventory.')
    return value
  }
  const inventory = await requireContainerControlCommand(
    engine,
    ['ps', '--all', '--quiet'],
    cwd,
    'Listing Compose container identifiers',
  )
  const ids = inventory.stdout.split('\n').map((value) => value.trim()).filter(Boolean)
  if (ids.length === 0) return []
  const inspected = await requireContainerControlCommand(
    engine,
    ['inspect', ...ids],
    cwd,
    'Inspecting Compose containers',
  )
  const value = JSON.parse(inspected.stdout)
  if (!Array.isArray(value)) throw new Error('Docker returned an invalid container inventory.')
  return value
}

function normalizedLabels(row) {
  if (
    row.Config?.Labels
    && typeof row.Config.Labels === 'object'
    && !Array.isArray(row.Config.Labels)
  ) return row.Config.Labels
  if (row.Labels && typeof row.Labels === 'object' && !Array.isArray(row.Labels)) {
    return row.Labels
  }
  const labels = {}
  for (const pair of String(row.Labels ?? '').split(',')) {
    const separator = pair.indexOf('=')
    if (separator > 0) labels[pair.slice(0, separator)] = pair.slice(separator + 1)
  }
  return labels
}

function oneService(rows, service) {
  const matches = rows.filter((row) => normalizedLabels(row)[COMPOSE_SERVICE] === service)
  if (matches.length !== 1) {
    throw new Error(`Fault-control requires exactly one ${service} service in the canonical Compose project.`)
  }
  return matches[0]
}

function requiredContainerId(row) {
  const value = row.Id ?? row.ID
  if (typeof value !== 'string' || !/^[a-f0-9]{12,64}$/i.test(value)) {
    throw new Error('Fault-control resolved an invalid container identifier.')
  }
  return value
}

function isRunning(row) {
  return row.State?.Running === true
    || String(row.State ?? '').toLowerCase() === 'running'
    || String(row.Status ?? '').toLowerCase().startsWith('up ')
}

export const FAULT_CONTROL_PATHS = ROUTES
