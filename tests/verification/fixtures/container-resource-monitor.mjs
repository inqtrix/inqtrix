import { resolve } from 'node:path'
import { isDeepStrictEqual } from 'node:util'

import { requireContainerControlCommand } from './container-control.mjs'

const COMPOSE_FILES = 'com.docker.compose.project.config_files'
const COMPOSE_PROJECT = 'com.docker.compose.project'
const COMPOSE_SERVICE = 'com.docker.compose.service'
const HEADROOM_MEMORY_PERCENT = 75
const HEADROOM_CPU_PERCENT_PER_CPU = 80
const HEADROOM_DATABASE_PERCENT = 80
const RECOVERY_MEMORY_GROWTH_PERCENT = 10

export class PodmanResourceMonitor {
  #baseline = null
  #containers = []
  #execute
  #machine = null
  #project
  #repositoryRoot
  #snapshots = []

  constructor({
    execute = requireContainerControlCommand,
    project,
    repositoryRoot,
  }) {
    if (typeof project !== 'string' || !/^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}$/.test(project)) {
      throw new Error('Resource monitoring requires an exact Compose project name.')
    }
    if (typeof repositoryRoot !== 'string' || repositoryRoot.length === 0) {
      throw new Error('Resource monitoring requires the repository root.')
    }
    this.#execute = execute
    this.#project = project
    this.#repositoryRoot = repositoryRoot
  }

  async initialize() {
    if (this.#baseline) throw new Error('Resource monitoring is already initialized.')
    const [machineResult, inventoryResult] = await Promise.all([
      this.#execute(
        'podman',
        ['machine', 'inspect'],
        this.#repositoryRoot,
        'Inspecting Podman machine capacity',
      ),
      this.#execute(
        'podman',
        [
          'ps',
          '--filter', `label=${COMPOSE_PROJECT}=${this.#project}`,
          '--format', 'json',
        ],
        this.#repositoryRoot,
        'Resolving canonical Compose resources',
      ),
    ])
    this.#machine = parseMachineCapacity(machineResult.stdout)
    this.#containers = parseComposeContainers(
      inventoryResult.stdout,
      this.#project,
      resolve(this.#repositoryRoot, 'deploy/compose/compose.stack.yaml'),
    )
    this.#baseline = await this.capture('baseline')
    assertResourceHeadroom(this.#baseline)
    return this.#baseline
  }

  async assertHeadroom(label) {
    if (!this.#baseline) throw new Error('Resource monitoring is not initialized.')
    const snapshot = await this.capture(label)
    assertResourceHeadroom(snapshot)
    return snapshot
  }

  async capture(label) {
    if (!this.#machine || this.#containers.length === 0) {
      throw new Error('Resource monitoring is not initialized.')
    }
    const ids = this.#containers.map((container) => container.id)
    const services = new Map(
      this.#containers.map((container) => [container.service, container]),
    )
    const postgres = services.get('postgres')
    const web = services.get('web')
    const collaboration = services.get('collaboration')
    const [statsResult, stateResult, inventoryResult, databaseResult, webSockets, collaborationSockets] = await Promise.all([
      this.#execute(
        'podman',
        ['stats', '--no-stream', '--format', 'json', ...ids],
        this.#repositoryRoot,
        'Sampling Compose resource use',
        null,
        30_000,
      ),
      this.#execute(
        'podman',
        ['inspect', '--format', '{{json .State}}', ...ids],
        this.#repositoryRoot,
        'Inspecting Compose runtime state',
      ),
      this.#execute(
        'podman',
        [
          'ps', '--all',
          '--filter', `label=${COMPOSE_PROJECT}=${this.#project}`,
          '--format', 'json',
        ],
        this.#repositoryRoot,
        'Inspecting Compose restart counters',
      ),
      this.#databaseConnections(postgres.id),
      this.#establishedSockets(web.id),
      this.#establishedSockets(collaboration.id),
    ])
    const snapshot = buildResourceSnapshot({
      containerInventory: inventoryResult.stdout,
      containers: this.#containers,
      databaseConnections: databaseResult.stdout,
      label,
      machine: this.#machine,
      socketCounts: {
        collaboration: collaborationSockets.stdout,
        web: webSockets.stdout,
      },
      states: stateResult.stdout,
      stats: statsResult.stdout,
    })
    this.#snapshots.push(snapshot)
    return snapshot
  }

  recovery(finalSnapshot, { memoryBaseline = this.#baseline } = {}) {
    if (!this.#baseline) throw new Error('Resource monitoring is not initialized.')
    return evaluateResourceRecovery(
      this.#baseline,
      finalSnapshot,
      this.#snapshots,
      { memoryBaseline },
    )
  }

  get snapshots() {
    return this.#snapshots.map((snapshot) => structuredClone(snapshot))
  }

  async #databaseConnections(containerId) {
    const query = [
      "SELECT count(*)::text || '/' || current_setting('max_connections')",
      'FROM pg_stat_activity',
    ].join(' ')
    return await this.#execute(
      'podman',
      [
        'exec', containerId, 'sh', '-c',
        'exec psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "$1"',
        'inqtrix-resource-monitor', query,
      ],
      this.#repositoryRoot,
      'Sampling PostgreSQL connection use',
    )
  }

  async #establishedSockets(containerId) {
    const script = [
      "awk 'NR > 1 && $4 == \"01\" { count += 1 } END { print count + 0 }'",
      '/proc/net/tcp /proc/net/tcp6',
    ].join(' ')
    return await this.#execute(
      'podman',
      ['exec', containerId, 'sh', '-c', script],
      this.#repositoryRoot,
      'Sampling established container sockets',
    )
  }
}

export function parseMachineCapacity(value) {
  const rows = parseJson(value, 'Podman machine inventory')
  if (!Array.isArray(rows) || rows.length !== 1 || rows[0]?.State !== 'running') {
    throw new Error('Resource monitoring requires exactly one running Podman machine.')
  }
  const cpus = Number(rows[0]?.Resources?.CPUs)
  const memoryMiB = Number(rows[0]?.Resources?.Memory)
  if (!Number.isSafeInteger(cpus) || cpus < 1 || !Number.isFinite(memoryMiB) || memoryMiB <= 0) {
    throw new Error('Podman machine capacity is invalid.')
  }
  return {
    cpus,
    memoryBytes: memoryMiB * 1024 * 1024,
  }
}

export function parseComposeContainers(value, project, canonicalCompose) {
  const rows = parseJson(value, 'Compose container inventory')
  if (!Array.isArray(rows)) throw new Error('Compose container inventory is invalid.')
  const containers = rows.map((row) => {
    const labels = normalizedLabels(row)
    const files = String(labels[COMPOSE_FILES] ?? '')
      .split(',')
      .map((entry) => resolve(entry.trim()))
    if (labels[COMPOSE_PROJECT] !== project || !files.includes(canonicalCompose)) {
      throw new Error('Resource monitoring refuses a container outside canonical Compose.')
    }
    const id = row.Id ?? row.ID
    const service = labels[COMPOSE_SERVICE]
    if (
      typeof id !== 'string'
      || !/^[a-f0-9]{12,64}$/i.test(id)
      || typeof service !== 'string'
      || !service
    ) throw new Error('Compose container identity is invalid.')
    return { id, service }
  })
  const services = new Set(containers.map((container) => container.service))
  for (const required of ['api', 'collaboration', 'postgres', 'web']) {
    if (!services.has(required)) {
      throw new Error(`Resource monitoring requires the ${required} service.`)
    }
  }
  if (services.size !== containers.length) {
    throw new Error('Resource monitoring requires one container per Compose service.')
  }
  return containers.sort((left, right) => left.service.localeCompare(right.service))
}

export function buildResourceSnapshot({
  containerInventory,
  containers,
  databaseConnections,
  label,
  machine,
  socketCounts,
  states,
  stats,
}) {
  if (typeof label !== 'string' || !label) throw new Error('Resource snapshot label is invalid.')
  const statsRows = parseJson(stats, 'Podman stats')
  const inventoryRows = parseJson(containerInventory, 'Compose restart inventory')
  if (!Array.isArray(statsRows) || !Array.isArray(inventoryRows)) {
    throw new Error('Podman resource samples are invalid.')
  }
  const stateRows = String(states).trim().split('\n').filter(Boolean).map(
    (row) => parseJson(row, 'container state'),
  )
  const selectedIds = new Set(containers.map((container) => container.id))
  const byStatsId = new Map(statsRows.map((row) => [normalizedId(row.id ?? row.ID), row]))
  const byInventoryId = new Map(
    inventoryRows.map((row) => [normalizedId(row.Id ?? row.ID), row]),
  )
  if (stateRows.length !== containers.length) {
    throw new Error('Container state sample is incomplete.')
  }
  const containerSamples = containers.map((container, index) => {
    const statsRow = findByPrefix(byStatsId, container.id)
    const inventoryRow = findByPrefix(byInventoryId, container.id)
    const state = stateRows[index]
    if (
      !statsRow
      || !inventoryRow
      || state?.Running !== true
      || state?.OOMKilled === true
      || state?.Dead === true
    ) throw new Error(`Compose service ${container.service} is not healthy enough for load.`)
    const memoryBytes = parseByteQuantity(String(statsRow.mem_usage ?? '').split('/', 1)[0])
    const cpuPercent = parsePercent(statsRow.cpu_percent)
    const pids = Number(statsRow.pids)
    const restarts = Number(inventoryRow.Restarts ?? 0)
    if (
      !Number.isSafeInteger(pids)
      || pids < 1
      || !Number.isSafeInteger(restarts)
      || restarts < 0
    ) throw new Error(`Compose service ${container.service} returned invalid process state.`)
    selectedIds.delete(container.id)
    return {
      cpuPercent,
      memoryBytes,
      pids,
      restarts,
      service: container.service,
    }
  })
  if (selectedIds.size !== 0) throw new Error('Resource snapshot omitted selected containers.')
  const database = parseDatabaseConnections(databaseConnections)
  const sockets = {
    collaboration: parseCount(socketCounts.collaboration, 'collaboration sockets'),
    web: parseCount(socketCounts.web, 'web sockets'),
  }
  const totalMemoryBytes = containerSamples.reduce(
    (total, container) => total + container.memoryBytes,
    0,
  )
  return {
    containers: containerSamples,
    cpuPercent: containerSamples.reduce(
      (total, container) => total + container.cpuPercent,
      0,
    ),
    database,
    label,
    machine,
    memoryPercent: (totalMemoryBytes / machine.memoryBytes) * 100,
    sampledAt: new Date().toISOString(),
    sockets,
    totalMemoryBytes,
  }
}

export function assertResourceHeadroom(snapshot) {
  const reasons = []
  if (snapshot.memoryPercent >= HEADROOM_MEMORY_PERCENT) {
    reasons.push(`memory ${snapshot.memoryPercent.toFixed(1)}% >= ${HEADROOM_MEMORY_PERCENT}%`)
  }
  const cpuLimit = snapshot.machine.cpus * HEADROOM_CPU_PERCENT_PER_CPU
  if (snapshot.cpuPercent >= cpuLimit) {
    reasons.push(`CPU ${snapshot.cpuPercent.toFixed(1)}% >= ${cpuLimit}%`)
  }
  const databasePercent = (snapshot.database.active / snapshot.database.maximum) * 100
  if (databasePercent >= HEADROOM_DATABASE_PERCENT) {
    reasons.push(`database connections ${databasePercent.toFixed(1)}% >= ${HEADROOM_DATABASE_PERCENT}%`)
  }
  if (reasons.length > 0) {
    throw new Error(`Load phase refused for insufficient resource headroom: ${reasons.join(', ')}.`)
  }
  return snapshot
}

export function evaluateResourceRecovery(
  baseline,
  finalSnapshot,
  snapshots,
  { memoryBaseline = baseline } = {},
) {
  if (!Array.isArray(snapshots) || snapshots.length === 0) {
    throw new Error('Resource recovery requires an ordered snapshot series.')
  }
  const memoryBaselineIndex = requireSnapshotIndex(
    snapshots,
    memoryBaseline,
    'Memory recovery baseline',
  )
  const finalSnapshotIndex = requireSnapshotIndex(
    snapshots,
    finalSnapshot,
    'Final resource snapshot',
  )
  if (memoryBaselineIndex > finalSnapshotIndex) {
    throw new Error('Memory recovery baseline must precede the final resource snapshot.')
  }
  const memorySnapshots = snapshots.slice(memoryBaselineIndex, finalSnapshotIndex + 1)
  assertChronologicalSnapshots(memorySnapshots)
  const baselineRestarts = new Map(
    baseline.containers.map((container) => [container.service, container.restarts]),
  )
  const restartGrowth = finalSnapshot.containers.filter(
    (container) => container.restarts !== baselineRestarts.get(container.service),
  )
  const coldToFinalMemoryGrowthPercent = memoryGrowthPercent(baseline, finalSnapshot)
  const recoveryMemoryGrowthPercent = memoryGrowthPercent(memoryBaseline, finalSnapshot)
  const peakMemoryPercent = Math.max(
    ...snapshots.map((snapshot) => snapshot.memoryPercent),
    finalSnapshot.memoryPercent,
  )
  const passed = (
    restartGrowth.length === 0
    && recoveryMemoryGrowthPercent <= RECOVERY_MEMORY_GROWTH_PERCENT
    && finalSnapshot.sockets.collaboration <= baseline.sockets.collaboration + 2
    && finalSnapshot.database.active < finalSnapshot.database.maximum * 0.8
    && finalSnapshot.memoryPercent < HEADROOM_MEMORY_PERCENT
  )
  return {
    collaborationSocketDelta: (
      finalSnapshot.sockets.collaboration - baseline.sockets.collaboration
    ),
    coldToFinalMemoryGrowthPercent,
    containerMemoryTrends: buildContainerMemoryTrends(
      memoryBaseline,
      finalSnapshot,
      memorySnapshots,
    ),
    databaseConnections: finalSnapshot.database,
    memoryBaselineLabel: memoryBaseline.label,
    memoryBaselineSampledAt: memoryBaseline.sampledAt,
    memoryGrowthLimitPercent: RECOVERY_MEMORY_GROWTH_PERCENT,
    memoryGrowthPercent: recoveryMemoryGrowthPercent,
    passed,
    peakMemoryPercent,
    restartedServices: restartGrowth.map((container) => container.service),
  }
}

function requireSnapshotIndex(snapshots, target, label) {
  const indexes = snapshots.flatMap((snapshot, index) => (
    snapshot === target || isDeepStrictEqual(snapshot, target)
      ? [index]
      : []
  ))
  if (indexes.length !== 1) {
    throw new Error(`${label} must identify exactly one recorded resource snapshot.`)
  }
  return indexes[0]
}

function assertChronologicalSnapshots(snapshots) {
  let previous = Number.NEGATIVE_INFINITY
  for (const snapshot of snapshots) {
    const sampledAt = Date.parse(snapshot.sampledAt)
    if (!Number.isFinite(sampledAt) || sampledAt < previous) {
      throw new Error('Resource recovery snapshots must have chronological timestamps.')
    }
    previous = sampledAt
  }
}

function memoryGrowthPercent(baseline, finalSnapshot) {
  return baseline.totalMemoryBytes === 0
    ? Number.POSITIVE_INFINITY
    : ((finalSnapshot.totalMemoryBytes / baseline.totalMemoryBytes) - 1) * 100
}

function buildContainerMemoryTrends(memoryBaseline, finalSnapshot, snapshots) {
  const baselineByService = new Map(
    memoryBaseline.containers.map((container) => [container.service, container]),
  )
  const finalByService = new Map(
    finalSnapshot.containers.map((container) => [container.service, container]),
  )
  return [...baselineByService.entries()].map(([service, baseline]) => {
    const final = finalByService.get(service)
    const samples = snapshots.map((snapshot) => {
      const container = snapshot.containers.find((candidate) => candidate.service === service)
      if (!container) {
        throw new Error(`Resource recovery snapshot omitted the ${service} service.`)
      }
      return {
        memoryBytes: container.memoryBytes,
        sampledAt: Date.parse(snapshot.sampledAt),
      }
    })
    if (!final) throw new Error(`Final resource snapshot omitted the ${service} service.`)
    const peakMemoryBytes = Math.max(...samples.map((sample) => sample.memoryBytes))
    const peakToFinalDropBytes = peakMemoryBytes - final.memoryBytes
    return {
      baselineMemoryBytes: baseline.memoryBytes,
      finalMemoryBytes: final.memoryBytes,
      growthBytes: final.memoryBytes - baseline.memoryBytes,
      growthPercent: baseline.memoryBytes === 0
        ? Number.POSITIVE_INFINITY
        : ((final.memoryBytes / baseline.memoryBytes) - 1) * 100,
      peakMemoryBytes,
      peakToFinalDropBytes,
      peakToFinalDropPercent: peakMemoryBytes === 0
        ? 0
        : (peakToFinalDropBytes / peakMemoryBytes) * 100,
      sampleCount: samples.length,
      service,
      slopeBytesPerMinute: linearMemorySlope(samples),
    }
  })
}

function linearMemorySlope(samples) {
  if (samples.length < 2) return null
  const origin = samples[0].sampledAt
  const points = samples.map((sample) => ({
    memoryBytes: sample.memoryBytes,
    minutes: (sample.sampledAt - origin) / 60_000,
  }))
  const meanMinutes = points.reduce((total, point) => total + point.minutes, 0)
    / points.length
  const meanMemory = points.reduce((total, point) => total + point.memoryBytes, 0)
    / points.length
  const denominator = points.reduce(
    (total, point) => total + ((point.minutes - meanMinutes) ** 2),
    0,
  )
  if (denominator === 0) return null
  return points.reduce(
    (total, point) => total
      + ((point.minutes - meanMinutes) * (point.memoryBytes - meanMemory)),
    0,
  ) / denominator
}

function parseDatabaseConnections(value) {
  const match = String(value).trim().match(/^([0-9]+)\/([0-9]+)$/)
  if (!match) throw new Error('PostgreSQL connection sample is invalid.')
  const active = Number(match[1])
  const maximum = Number(match[2])
  if (!Number.isSafeInteger(active) || !Number.isSafeInteger(maximum) || maximum < 1) {
    throw new Error('PostgreSQL connection sample is invalid.')
  }
  return { active, maximum }
}

function parseCount(value, label) {
  const parsed = Number(String(value).trim())
  if (!Number.isSafeInteger(parsed) || parsed < 0) throw new Error(`${label} sample is invalid.`)
  return parsed
}

function parsePercent(value) {
  const parsed = Number(String(value).trim().replace(/%$/, ''))
  if (!Number.isFinite(parsed) || parsed < 0) throw new Error('Container CPU sample is invalid.')
  return parsed
}

function parseByteQuantity(value) {
  const match = String(value).trim().match(
    /^([0-9]+(?:\.[0-9]+)?)\s*(B|kB|MB|GB|TB|KiB|MiB|GiB|TiB)$/,
  )
  if (!match) throw new Error('Container memory sample is invalid.')
  const base = match[2].includes('i') ? 1024 : 1000
  const exponent = ['B', 'kB', 'KiB', 'MB', 'MiB', 'GB', 'GiB', 'TB', 'TiB']
    .findIndex((unit) => unit === match[2])
  const powers = {
    B: 0,
    GB: 3,
    GiB: 3,
    kB: 1,
    KiB: 1,
    MB: 2,
    MiB: 2,
    TB: 4,
    TiB: 4,
  }
  if (exponent < 0) throw new Error('Container memory unit is invalid.')
  return Number(match[1]) * (base ** powers[match[2]])
}

function findByPrefix(index, id) {
  const matches = [...index.entries()].filter(
    ([candidate]) => id.startsWith(candidate) || candidate.startsWith(id),
  )
  return matches.length === 1 ? matches[0][1] : null
}

function normalizedId(value) {
  return typeof value === 'string' && /^[a-f0-9]{12,64}$/i.test(value)
    ? value
    : ''
}

function normalizedLabels(row) {
  if (row?.Labels && typeof row.Labels === 'object' && !Array.isArray(row.Labels)) {
    return row.Labels
  }
  return {}
}

function parseJson(value, label) {
  try {
    return JSON.parse(String(value).trim())
  } catch {
    throw new Error(`${label} is not valid JSON.`)
  }
}
