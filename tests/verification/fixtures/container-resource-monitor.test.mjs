import assert from 'node:assert/strict'
import { describe, test } from 'node:test'

import {
  assertResourceHeadroom,
  buildResourceSnapshot,
  evaluateResourceRecovery,
  parseComposeContainers,
  parseMachineCapacity,
} from './container-resource-monitor.mjs'

const canonicalCompose = '/repo/deploy/compose/compose.stack.yaml'
const project = 'inqtrix-test'
const services = ['api', 'collaboration', 'postgres', 'web']
const containers = services.map((service, index) => ({
  id: `${String(index + 1).repeat(64)}`,
  service,
}))

describe('resource-aware local load guard', () => {
  test('selects one running canonical Compose project and machine capacity', () => {
    const inventory = services.map((service, index) => ({
      Id: containers[index].id,
      Labels: {
        'com.docker.compose.project': project,
        'com.docker.compose.project.config_files': canonicalCompose,
        'com.docker.compose.service': service,
      },
    }))
    assert.deepEqual(
      parseComposeContainers(JSON.stringify(inventory), project, canonicalCompose),
      containers,
    )
    assert.deepEqual(parseMachineCapacity(JSON.stringify([{
      Resources: { CPUs: 7, Memory: 11444 },
      State: 'running',
    }])), {
      cpus: 7,
      memoryBytes: 11444 * 1024 * 1024,
    })
  })

  test('rejects foreign Compose files and missing critical services', () => {
    const row = {
      Id: containers[0].id,
      Labels: {
        'com.docker.compose.project': project,
        'com.docker.compose.project.config_files': '/tmp/other.yaml',
        'com.docker.compose.service': 'api',
      },
    }
    assert.throws(
      () => parseComposeContainers(JSON.stringify([row]), project, canonicalCompose),
      /outside canonical Compose/,
    )
  })

  test('measures headroom and requires post-quiet resource recovery', () => {
    const snapshot = resourceSnapshot('baseline', [
      '400MB / 12GB',
      '200MB / 12GB',
      '300MB / 12GB',
      '100MB / 12GB',
    ])
    assert.doesNotThrow(() => assertResourceHeadroom(snapshot))

    const recovered = resourceSnapshot('final', [
      '420MB / 12GB',
      '205MB / 12GB',
      '300MB / 12GB',
      '105MB / 12GB',
    ])
    const result = evaluateResourceRecovery(snapshot, recovered, [snapshot, recovered])
    assert.equal(result.passed, true)
    assert(result.memoryGrowthPercent < 10)

    const leaked = resourceSnapshot('leaked', [
      '700MB / 12GB',
      '400MB / 12GB',
      '300MB / 12GB',
      '100MB / 12GB',
    ], { collaborationSockets: 8 })
    assert.equal(
      evaluateResourceRecovery(snapshot, leaked, [snapshot, leaked]).passed,
      false,
    )
  })

  test('separates startup warmup from steady-state memory recovery', () => {
    const cold = sampledAt(resourceSnapshot('baseline', [
      '400MB / 12GB',
      '200MB / 12GB',
      '300MB / 12GB',
      '100MB / 12GB',
    ]), '2026-01-01T00:00:00.000Z')
    const warm = sampledAt(resourceSnapshot('before-latency-100ms', [
      '500MB / 12GB',
      '250MB / 12GB',
      '350MB / 12GB',
      '100MB / 12GB',
    ], { collaborationSockets: 20 }), '2026-01-01T00:05:00.000Z')
    const peak = sampledAt(resourceSnapshot('before-packet-loss-1pct', [
      '520MB / 12GB',
      '260MB / 12GB',
      '360MB / 12GB',
      '110MB / 12GB',
    ]), '2026-01-01T00:20:00.000Z')
    const final = sampledAt(resourceSnapshot('post-quiet', [
      '500MB / 12GB',
      '255MB / 12GB',
      '355MB / 12GB',
      '110MB / 12GB',
    ], { collaborationSockets: 4 }), '2026-01-01T00:35:30.000Z')

    const result = evaluateResourceRecovery(
      cold,
      final,
      [cold, warm, peak, final],
      { memoryBaseline: warm },
    )

    assert.equal(result.passed, true)
    assert(result.coldToFinalMemoryGrowthPercent > 20)
    assert(result.memoryGrowthPercent < 2)
    assert.equal(result.memoryGrowthLimitPercent, 10)
    assert.equal(result.memoryBaselineLabel, 'before-latency-100ms')
    assert.equal(result.collaborationSocketDelta, 2)
    assert.deepEqual(result.restartedServices, [])
    assert.equal(result.containerMemoryTrends.length, 4)
    const api = result.containerMemoryTrends.find((trend) => trend.service === 'api')
    assert.equal(api.baselineMemoryBytes, 500_000_000)
    assert.equal(api.finalMemoryBytes, 500_000_000)
    assert.equal(api.peakMemoryBytes, 520_000_000)
    assert.equal(api.peakToFinalDropBytes, 20_000_000)
    assert.equal(api.sampleCount, 3)
    assert.equal(typeof api.slopeBytesPerMinute, 'number')
  })

  test('fails sustained memory growth after the warm baseline', () => {
    const cold = sampledAt(resourceSnapshot('baseline', [
      '400MB / 12GB',
      '200MB / 12GB',
      '300MB / 12GB',
      '100MB / 12GB',
    ]), '2026-01-01T00:00:00.000Z')
    const warm = sampledAt(resourceSnapshot('before-latency-100ms', [
      '500MB / 12GB',
      '250MB / 12GB',
      '350MB / 12GB',
      '100MB / 12GB',
    ]), '2026-01-01T00:05:00.000Z')
    const growing = sampledAt(resourceSnapshot('before-packet-loss-1pct', [
      '550MB / 12GB',
      '275MB / 12GB',
      '375MB / 12GB',
      '100MB / 12GB',
    ]), '2026-01-01T00:20:00.000Z')
    const final = sampledAt(resourceSnapshot('post-quiet', [
      '600MB / 12GB',
      '300MB / 12GB',
      '400MB / 12GB',
      '100MB / 12GB',
    ]), '2026-01-01T00:35:30.000Z')

    const result = evaluateResourceRecovery(
      cold,
      final,
      [cold, warm, growing, final],
      { memoryBaseline: warm },
    )

    assert.equal(result.passed, false)
    assert(result.memoryGrowthPercent > 10)
    assert(
      result.containerMemoryTrends
        .filter((trend) => trend.service !== 'web')
        .every((trend) => trend.slopeBytesPerMinute > 0),
    )
  })

  test('keeps restart checks anchored to the cold baseline', () => {
    const cold = sampledAt(resourceSnapshot('baseline', [
      '400MB / 12GB',
      '200MB / 12GB',
      '300MB / 12GB',
      '100MB / 12GB',
    ]), '2026-01-01T00:00:00.000Z')
    const warm = sampledAt(resourceSnapshot('before-latency-100ms', [
      '410MB / 12GB',
      '205MB / 12GB',
      '300MB / 12GB',
      '100MB / 12GB',
    ], { restarts: { api: 1 } }), '2026-01-01T00:05:00.000Z')
    const final = sampledAt(resourceSnapshot('post-quiet', [
      '410MB / 12GB',
      '205MB / 12GB',
      '300MB / 12GB',
      '100MB / 12GB',
    ], { restarts: { api: 1 } }), '2026-01-01T00:35:30.000Z')

    const result = evaluateResourceRecovery(
      cold,
      final,
      [cold, warm, final],
      { memoryBaseline: warm },
    )

    assert.equal(result.passed, false)
    assert.deepEqual(result.restartedServices, ['api'])
  })

  test('rejects a memory baseline that was not recorded in the run', () => {
    const cold = resourceSnapshot('baseline', [
      '400MB / 12GB',
      '200MB / 12GB',
      '300MB / 12GB',
      '100MB / 12GB',
    ])
    const final = resourceSnapshot('post-quiet', [
      '410MB / 12GB',
      '205MB / 12GB',
      '300MB / 12GB',
      '100MB / 12GB',
    ])
    const invented = structuredClone(cold)
    invented.containers[0].memoryBytes += 1
    invented.containers[1].memoryBytes -= 1

    assert.throws(
      () => evaluateResourceRecovery(
        cold,
        final,
        [cold, final],
        { memoryBaseline: invented },
      ),
      /must identify exactly one recorded resource snapshot/,
    )
  })

  test('refuses phases at machine or database saturation', () => {
    const saturated = resourceSnapshot('saturated', [
      '3GB / 4GB',
      '1GB / 4GB',
      '500MB / 4GB',
      '500MB / 4GB',
    ], { database: '90/100', machineMemoryMiB: 4096 })
    assert.throws(() => assertResourceHeadroom(saturated), /insufficient resource headroom/)
  })
})

function resourceSnapshot(label, memory, options = {}) {
  const inventory = containers.map((container) => ({
    Id: container.id,
    Restarts: options.restarts?.[container.service] ?? 0,
  }))
  const stats = containers.map((container, index) => ({
    cpu_percent: '5%',
    id: container.id.slice(0, 12),
    mem_usage: memory[index],
    pids: '5',
  }))
  const states = containers.map(() => JSON.stringify({
    Dead: false,
    OOMKilled: false,
    Running: true,
  })).join('\n')
  return buildResourceSnapshot({
    containerInventory: JSON.stringify(inventory),
    containers,
    databaseConnections: options.database ?? '20/100',
    label,
    machine: {
      cpus: 7,
      memoryBytes: (options.machineMemoryMiB ?? 11444) * 1024 * 1024,
    },
    socketCounts: {
      collaboration: String(options.collaborationSockets ?? 2),
      web: '3',
    },
    states,
    stats: JSON.stringify(stats),
  })
}

function sampledAt(snapshot, value) {
  snapshot.sampledAt = value
  return snapshot
}
