import process from 'node:process'
import { chmod, rename, writeFile } from 'node:fs/promises'
import { pathToFileURL } from 'node:url'

import {
  API_DEGRADATION_LIMIT_PERCENT,
  API_P95_LIMIT_MS,
  API_RELATIVE_GATE_MIN_BASELINE_MS,
  FatalSocketState,
  CAPACITY_MIN_ACK_ROUNDS_PER_WRITER,
  CAPACITY_MIN_DURATION_MS,
  CAPACITY_OBSERVER_COHORT,
  SOAK_MIN_ACK_ROUNDS_PER_WRITER,
  SOAK_MIN_DURATION_MS,
  RawCollaborationClient,
  SessionRotationSupervisor,
  allLoadGatesPassed,
  assertCapacityPreflight,
  assertSoakPreflight,
  connectInBatches,
  delay,
  evaluateGates,
  loadFixture,
  measureApiProbe,
  parseArguments,
  performUngracefulRestart,
  prepareSessions,
  reissueSessions,
  resolveApiProbe,
  resolveInstanceProbe,
  resolveNetworkControl,
  resolveRestartControl,
  resolveSessionReissueControl,
  runSustainedWriterLoad,
  runSoakPhases,
  summarize,
  summarizeApiProbe,
  verifyObserverCohort,
} from './collaboration-load-lib.mjs'

export async function main(args = process.argv.slice(2), environment = process.env) {
  const options = parseArguments(args, environment)
  if (options.help) {
    printHelp()
    return
  }
  if (!options.fixturePath) {
    throw new Error(
      'No lease/session fixture supplied. Pass --fixture PATH or set INQTRIX_LOAD_SESSION_FIXTURE.',
    )
  }

  const fixture = loadFixture(options.fixturePath)
  const sessions = prepareSessions(fixture, options)
  const apiProbeConfiguration = resolveApiProbe(fixture, options)
  const instanceProbe = resolveInstanceProbe(fixture, options)
  const restartControl = resolveRestartControl(fixture, options, environment)
  const networkControl = resolveNetworkControl(fixture, options, environment)
  const sessionReissueControl = resolveSessionReissueControl(fixture, options, environment)
  if ((restartControl === null) !== (instanceProbe === null)) {
    throw new Error(
      'fixture.restart_control and fixture.instance_probe must be supplied together.',
    )
  }
  assertCapacityPreflight(
    options,
    sessions,
    apiProbeConfiguration,
    restartControl,
    instanceProbe,
    sessionReissueControl,
  )
  assertSoakPreflight(
    options,
    sessions,
    apiProbeConfiguration,
    networkControl,
    sessionReissueControl,
  )

  let baselineApiMeasurement = null
  if (apiProbeConfiguration) {
    progress('Measuring baseline API latency (20 samples)...', options.json)
    baselineApiMeasurement = await measureApiProbe(apiProbeConfiguration)
  }

  const fatal = new FatalSocketState()
  const clients = sessions.map((session, index) => new RawCollaborationClient({
    allowInsecureTls: options.allowInsecureTls,
    index,
    onFatal: (error) => fatal.record(error),
    session,
  }))
  let freshObservers = []
  let clientsClosed = false
  let rotationStopped = false
  let rotationSupervisor = null

  try {
    progress(
      `Connecting ${clients.length} ${options.mode} clients in batches of ${options.connectConcurrency}...`,
      options.json,
    )
    await connectInBatches(
      clients,
      options.connectConcurrency,
      options.connectTimeoutMs,
      fatal,
      (connected, total) => progress(`Connected ${connected}/${total}`, options.json),
    )

    const writers = clients.filter((client) => client.session.access === 'edit')
      .slice(0, options.writers)
    if (writers.length !== options.writers) {
      throw new Error(
        `Fixture provides ${writers.length} edit-capable sessions; ${options.writers} writers are required.`,
      )
    }
    const writerSet = new Set(writers)
    const observers = clients.filter((client) => !writerSet.has(client))
      .slice(0, options.observers)
    if (observers.length !== options.observers) {
      throw new Error(
        `Fixture provides ${observers.length} non-writer observers; ${options.observers} are required.`,
      )
    }

    let connectedRotations = 0
    let scheduledRotations = 0
    if (sessionReissueControl) {
      progress(
        `Rotating leases for all ${clients.length} connected clients through authenticated session reissue...`,
        options.json,
      )
      rotationSupervisor = new SessionRotationSupervisor({
        clients,
        concurrency: options.connectConcurrency,
        control: sessionReissueControl,
        fatal,
        timeoutMs: options.connectTimeoutMs,
      })
      connectedRotations = await rotationSupervisor.rotateNow('connected_rotation')
      rotationSupervisor.start()
    }

    progress(
      apiProbeConfiguration
        ? `Sustaining ${writers.length} writers across ${observers.length} observers while the loaded API probe runs...`
        : `Running ${writers.length} writers with the API gate explicitly skipped...`,
      options.json,
    )
    let load
    let apiProbe
    let gates
    if (options.mode === 'soak') {
      progress('Running six separately budgeted five-minute network phases...', options.json)
      load = await runSoakPhases({
        apiProbe: apiProbeConfiguration,
        baselineApiMeasurement,
        fatal,
        networkControl,
        observers,
        sampleTimeoutMs: options.sampleTimeoutMs,
        writerIntervalMs: options.writerIntervalMs,
        writers,
      })
      apiProbe = {
        absoluteLimitMs: API_P95_LIMIT_MS,
        phases: load.phases.map((phase) => ({
          ...phase.apiProbe,
          id: phase.id,
        })),
        relativeGateMinBaselineMs: API_RELATIVE_GATE_MIN_BASELINE_MS,
        relativeLimitPercent: API_DEGRADATION_LIMIT_PERCENT,
        status: load.gates.apiLatencyStatus,
      }
      gates = load.gates
    } else {
      load = await runSustainedWriterLoad({
        apiProbe: apiProbeConfiguration,
        fatal,
        minAckRoundsPerWriter: options.minAckRoundsPerWriter,
        minDurationMs: options.minDurationMs,
        observers,
        sampleTimeoutMs: options.sampleTimeoutMs,
        writerIntervalMs: options.writerIntervalMs,
        writers,
      })
      apiProbe = summarizeApiProbe(
        baselineApiMeasurement?.latencies ?? null,
        load.loadedApiLatencies,
      )
      gates = evaluateGates(
        load.visibleLatencies,
        load.durableLatencies,
        apiProbe,
        options,
        load,
      )
    }

    fatal.throwIfSet()
    await delay(options.postSampleQuietMs)
    fatal.throwIfSet()
    if (rotationSupervisor) {
      const rotations = await rotationSupervisor.stop()
      rotationStopped = true
      connectedRotations = rotations.connected
      scheduledRotations = rotations.scheduled
    }
    let restart = null
    if (restartControl) {
      progress(
        'Ungracefully restarting the collaboration sidecar while original sockets remain open...',
        options.json,
      )
      const exercised = await performUngracefulRestart(
        restartControl,
        observers[0].session.room,
        clients,
        options.connectTimeoutMs,
        instanceProbe,
      )
      restart = {
        afterEpoch: exercised.transition.after.epoch,
        beforeEpoch: exercised.transition.before.epoch,
        closedSockets: exercised.closedSockets,
        epochAdvanced: exercised.transition.after.epoch > exercised.transition.before.epoch,
        instanceIdentityChanged: (
          exercised.transition.after.instanceId !== exercised.transition.before.instanceId
        ),
        kind: exercised.transition.restartKind,
      }
    }
    await Promise.all(clients.map((client) => client.close()))
    clientsClosed = true

    let freshObserverSessions
    try {
      freshObserverSessions = sessionReissueControl
        ? await reissueSessions(
            sessionReissueControl,
            observers.map((observer) => observer.session),
            restart ? 'post_restart_observer' : 'fresh_observer',
          )
        : observers.map((observer) => observer.session)
    } catch (error) {
      fatal.record(error)
      throw error
    }
    freshObservers = freshObserverSessions.map((session, index) => new RawCollaborationClient({
      allowInsecureTls: options.allowInsecureTls,
      index: clients.length + index,
      onFatal: (error) => fatal.record(error),
      session,
    }))
    await connectInBatches(
      freshObservers,
      Math.min(options.connectConcurrency, freshObservers.length),
      options.connectTimeoutMs,
      fatal,
    )
    const reconstruction = verifyObserverCohort(
      freshObservers,
      load.markers,
      load.runId,
    )
    await delay(Math.min(options.postSampleQuietMs, 500))
    fatal.throwIfSet()
    await Promise.all(freshObservers.map((observer) => observer.close()))
    freshObservers = []

    const result = {
      apiProbe,
      connections: clients.length,
      durableAckMs: summarize(load.durableLatencies),
      gates,
      mode: options.mode,
      phases: load.phases ?? null,
      reconstruction: {
        ...reconstruction,
        restart,
      },
      sessionRotation: {
        connectedClients: connectedRotations,
        freshObservers: sessionReissueControl ? freshObserverSessions.length : 0,
        passed: (
          options.mode === 'smoke'
          || (options.mode === 'capacity' && (
            connectedRotations === clients.length
            && freshObserverSessions.length === observers.length
          ))
          || (options.mode === 'soak' && (
            connectedRotations === clients.length
            && scheduledRotations >= clients.length
            && freshObserverSessions.length === observers.length
          ))
        ),
        scheduledClients: scheduledRotations,
      },
      sustainedDurationMs: load.durationMs,
      loadedApiSampleSpanMs: load.loadedApiSampleSpanMs,
      visibleUpdateMs: summarize(load.visibleLatencies),
      writeSamples: load.markers.length,
      writerRounds: {
        maximum: Math.max(...load.roundsPerWriter),
        minimum: Math.min(...load.roundsPerWriter),
      },
      writers: writers.length,
    }
    const passed = allLoadGatesPassed(
      gates,
      reconstruction,
      result.sessionRotation,
    )
    result.passed = passed
    await writeScenarioResults(
      options.mode,
      gates,
      reconstruction,
      result.sessionRotation,
      environment,
    )
    printResult(result, options.json)
    // In ramp mode a rung that misses its budget is the EXPECTED way the
    // ladder ends: the engine decides whether that is an honest local
    // ceiling or an integrity failure. Letting one rung set a failing
    // exit code here would turn every controlled stop red.
    if (!passed && options.mode !== 'ramp') {
      process.exitCode = 1
    }
    return result
  } finally {
    let rotationStopError = null
    if (rotationSupervisor && !rotationStopped) {
      try {
        await rotationSupervisor.stop()
      } catch (error) {
        rotationStopError = error
      }
    }
    if (freshObservers.length > 0) {
      await Promise.allSettled(freshObservers.map((observer) => observer.close()))
    }
    if (!clientsClosed) await Promise.allSettled(clients.map((client) => client.close()))
    if (rotationStopError) throw rotationStopError
  }
}

export async function writeScenarioResults(
  mode,
  gates,
  reconstruction,
  sessionRotation,
  environment,
  supplemental = {},
) {
  const path = environment.INQTRIX_VERIFICATION_SCENARIO_RESULTS_PATH
  if (!path) return
  const latencyPassed = (
    gates.visibleUpdatePassed
    && gates.durableAckPassed
    && gates.apiLatencyStatus !== 'failed'
    && gates.apiSampleSpanPassed !== false
    && gates.minimumAckRoundsPassed
    && gates.minimumDurationPassed
    && gates.observerCohortPassed
  )
  // The ramp runs many rungs through this entrypoint; only the engine
  // knows whether a stopped rung is an honest local ceiling or a real
  // failure, so it owns the sidecar and this call stays silent.
  if (mode === 'ramp') return
  const scenarios = mode === 'capacity'
    ? [
        {
          id: 'load-capacity.latency',
          status: latencyPassed ? 'passed' : 'failed',
        },
        {
          id: 'load-capacity.rotation',
          status: sessionRotation.passed ? 'passed' : 'failed',
        },
        {
          id: 'load-capacity.restart',
          status: reconstruction.passed ? 'passed' : 'failed',
        },
      ]
    : mode === 'soak'
      ? [
          {
            id: 'load-soak.identity-matrix',
            status: supplemental.identityMatrixPassed === true ? 'passed' : 'failed',
          },
          {
            id: 'load-soak.comments-and-navigation',
            status: supplemental.commentsAndNavigationPassed === true ? 'passed' : 'failed',
          },
          {
            id: 'load-soak.network-phases',
            status: gates.phaseResultsPassed === true ? 'passed' : 'failed',
          },
          {
            id: 'load-soak.durability',
            status: (
              latencyPassed
              && reconstruction.passed
              && sessionRotation.passed
            ) ? 'passed' : 'failed',
          },
          {
            id: 'load-soak.feature-activity',
            status: supplemental.featureActivityPassed === true ? 'passed' : 'failed',
          },
          {
            id: 'load-soak.resource-recovery',
            status: supplemental.resourceRecoveryPassed === true ? 'passed' : 'failed',
          },
        ]
      : [
        { id: 'load-smoke.protocol', status: 'passed' },
        {
          id: 'load-smoke.durability',
          status: latencyPassed ? 'passed' : 'failed',
        },
        {
          id: 'load-smoke.reconstruction',
          status: reconstruction.passed ? 'passed' : 'failed',
        },
      ]
  await writeScenarioSidecar(path, scenarios)
}

/** Atomic 0600 sidecar in the canonical {schemaVersion, scenarios} shape.
 * Shared so aggregating engines write exactly the same contract. */
export async function writeScenarioSidecar(path, scenarios) {
  if (!path) return
  const temporaryPath = `${path}.tmp`
  await writeFile(temporaryPath, `${JSON.stringify({
    scenarios,
    schemaVersion: 1,
  }, null, 2)}\n`, { encoding: 'utf8', mode: 0o600 })
  await rename(temporaryPath, path)
  await chmod(path, 0o600)
}

function printHelp() {
  process.stdout.write('Usage: npm run verify:load-smoke\n')
  process.stdout.write('       npm run verify:load-soak\n')
  process.stdout.write('       npm run verify:load-capacity -- --fixture PATH\n\n')
  process.stdout.write(`Soak: exactly 25 identities across six five-minute network phases, at least ${SOAK_MIN_DURATION_MS}ms and ${SOAK_MIN_ACK_ROUNDS_PER_WRITER} acknowledged rounds/writer.\n`)
  process.stdout.write(`Capacity: exactly 1000 connections, 100 writers, ${CAPACITY_OBSERVER_COHORT} observers, at least ${CAPACITY_MIN_DURATION_MS}ms and ${CAPACITY_MIN_ACK_ROUNDS_PER_WRITER} acknowledged rounds/writer.\n`)
  process.stdout.write('Capacity latency: visible p95 <250ms, durable p95 <500ms, FastAPI /health degradation <=20%.\n')
  process.stdout.write('Internal smoke-engine options: --connections N --writers N --observers N --connect-concurrency N\n')
  process.stdout.write('  --connect-timeout-ms N --sample-timeout-ms N --post-sample-quiet-ms N\n')
  process.stdout.write('  --min-duration-ms N --min-ack-rounds N\n')
  process.stdout.write('  --visible-p95-ms N --durable-p95-ms N --allow-insecure-tls\n')
  process.stdout.write('  --skip-api-probe --json\n')
}

function printResult(result, json) {
  if (json) {
    process.stdout.write(`${JSON.stringify(result)}\n`)
    return
  }
  process.stdout.write(`Collaboration load result (${result.mode})\n`)
  process.stdout.write(`  connections: ${result.connections}\n`)
  process.stdout.write(`  writers: ${result.writers}\n`)
  process.stdout.write(`  observers: ${result.reconstruction.observerCount}\n`)
  process.stdout.write(`  write samples: ${result.writeSamples}\n`)
  if (result.mode === 'soak') {
    process.stdout.write(`  sustained: ${formatMs(result.sustainedDurationMs)} gate>=${SOAK_MIN_DURATION_MS}ms ${result.gates.minimumDurationPassed ? 'PASS' : 'FAIL'}\n`)
    process.stdout.write(`  acknowledged rounds/writer: min=${result.writerRounds.minimum} max=${result.writerRounds.maximum} gate>=${SOAK_MIN_ACK_ROUNDS_PER_WRITER} ${result.gates.minimumAckRoundsPassed ? 'PASS' : 'FAIL'}\n`)
    for (const phase of result.phases) {
      process.stdout.write(`  phase ${phase.id}: visible=[${formatVisibleUpdateGate(phase.visibleUpdateMs, phase.gates)}] durable-p95=${formatMs(phase.durableAckMs.p95)} gate<${phase.gates.durableAckP95Ms}ms api=[${formatApiProbe(phase.apiProbe)}] ${phase.passed ? 'PASS' : 'FAIL'}\n`)
    }
  } else {
    process.stdout.write(`  sustained: ${formatMs(result.sustainedDurationMs)} gate>=${result.gates.minimumDurationMs}ms ${result.gates.minimumDurationPassed ? 'PASS' : 'FAIL'}\n`)
    process.stdout.write(`  acknowledged rounds/writer: min=${result.writerRounds.minimum} max=${result.writerRounds.maximum} gate>=${result.gates.minimumAckRounds} ${result.gates.minimumAckRoundsPassed ? 'PASS' : 'FAIL'}\n`)
    process.stdout.write(`  visible-update: ${formatVisibleUpdateGate(result.visibleUpdateMs, result.gates)}\n`)
    process.stdout.write(`  durable-ack: p50=${formatMs(result.durableAckMs.p50)} p95=${formatMs(result.durableAckMs.p95)} gate<${result.gates.durableAckP95Ms}ms ${result.gates.durableAckPassed ? 'PASS' : 'FAIL'}\n`)
    if (result.apiProbe.status === 'skipped') {
      process.stdout.write('  api-latency: SKIPPED (explicit load-smoke API-probe opt-out)\n')
    } else {
      process.stdout.write(`  api-latency: ${formatApiProbe(result.apiProbe)}\n`)
      process.stdout.write(`  api-sample-span: ${formatMs(result.loadedApiSampleSpanMs)} gate>=${result.gates.minimumDurationMs}ms ${result.gates.apiSampleSpanPassed ? 'PASS' : 'FAIL'}\n`)
    }
  }
  const restart = result.reconstruction.restart
  process.stdout.write(`  reconstruction: observers=${result.reconstruction.observerCount} expected-per-observer=${result.reconstruction.expectedPerObserver} failed-observers=${result.reconstruction.failedObservers} missing=${result.reconstruction.missing} duplicates=${result.reconstruction.duplicates} unexpected=${result.reconstruction.unexpected} ${result.reconstruction.passed ? 'PASS' : 'FAIL'}\n`)
  process.stdout.write(`  restart: ${restart ? `${restart.kind} sockets=${restart.closedSockets} instance-changed=${restart.instanceIdentityChanged} epoch=${restart.beforeEpoch}->${restart.afterEpoch}` : 'not exercised'} ${restart?.instanceIdentityChanged && restart?.epochAdvanced ? 'PASS' : result.mode === 'capacity' ? 'FAIL' : 'SKIPPED'}\n`)
  process.stdout.write(`  lease-rotation: connected=${result.sessionRotation.connectedClients} scheduled=${result.sessionRotation.scheduledClients} fresh-observers=${result.sessionRotation.freshObservers} ${result.sessionRotation.passed ? 'PASS' : 'FAIL'}\n`)
}

function progress(message, json) {
  const stream = json ? process.stderr : process.stdout
  stream.write(`${message}\n`)
}

function formatMs(value) {
  return `${value.toFixed(1)}ms`
}

function formatPercent(value) {
  return `${value.toFixed(1)}%`
}

export function formatApiProbe(apiProbe) {
  const relativeGate = apiProbe.relativeGateApplied
    ? `${apiProbe.relativePassed ? 'PASS' : 'FAIL'} (active)`
    : apiProbe.relativePassed
      ? `PASS (advisory; baseline below ${API_RELATIVE_GATE_MIN_BASELINE_MS}ms)`
      : `WARN (advisory; baseline below ${API_RELATIVE_GATE_MIN_BASELINE_MS}ms)`
  return `baseline-p95=${formatMs(apiProbe.baselineP95Ms)} loaded-p95=${formatMs(apiProbe.loadedP95Ms)} degradation=${formatPercent(apiProbe.degradationPercent)} absolute<=${API_P95_LIMIT_MS}ms ${apiProbe.absolutePassed ? 'PASS' : 'FAIL'} relative<=${API_DEGRADATION_LIMIT_PERCENT}% ${relativeGate} status=${apiProbe.status.toUpperCase()}`
}

export function formatVisibleUpdateGate(visibleUpdateMs, gates) {
  const limits = gates.visibleUpdateWarningEnabled
    ? `target<${gates.visibleUpdateTargetP95Ms}ms hard<=${gates.visibleUpdateHardLimitP95Ms}ms`
    : `gate<${gates.visibleUpdateTargetP95Ms}ms`
  return `p50=${formatMs(visibleUpdateMs.p50)} p95=${formatMs(visibleUpdateMs.p95)} ${limits} status=${gates.visibleUpdateStatus.toUpperCase()}`
}

const entryPoint = process.argv[1] ? pathToFileURL(process.argv[1]).href : null
if (entryPoint === import.meta.url) {
  main().catch((error) => {
    const message = error instanceof Error
      ? error.message
      : 'Unknown collaboration load-test failure.'
    process.stderr.write(`ERROR: ${message}\n`)
    process.exitCode = 1
  })
}
