import { randomBytes } from 'node:crypto'
import { writeFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { resolve } from 'node:path'
import process from 'node:process'
import { setTimeout as delay } from 'node:timers/promises'

import {
  main as runCollaborationLoad,
  writeScenarioResults,
  writeScenarioSidecar,
} from '../../load/collaboration-load.mjs'
import {
  RAMP_MAX_IDENTITIES,
  RAMP_SESSIONS_PER_IDENTITY,
  partitionRampStages,
  planRampStages,
  summarizeRamp,
} from '../../load/collaboration-load-lib.mjs'
import {
  eraseTemporaryUser,
  ensureTemporaryUsers,
  temporaryUserDescriptors,
} from '../fixtures/accounts.mjs'
import { assertFixture, fetchActorJson } from '../fixtures/api.mjs'
import { createApiSessionFixtures } from '../fixtures/api-sessions.mjs'
import { PodmanResourceMonitor } from '../fixtures/container-resource-monitor.mjs'
import {
  createCollaborationDocument,
  deleteCollaborationDocument,
} from '../fixtures/documents.mjs'
import { resolveFaultControlContainers } from '../fixtures/fault-control-server.mjs'
import {
  createRegisteredSessionReissuer,
  startLoadControlServer,
} from '../fixtures/load-control-server.mjs'
import { LoadSoakProductActivity } from '../fixtures/load-soak-activity.mjs'
import { runAndFinalizeLoadSoakEvidence } from '../fixtures/load-soak-finalization.mjs'
import {
  buildLoadDocumentSeed,
  buildLoadRampFixture,
  buildLoadSmokeFixture,
  buildLoadSoakFixture,
  LOAD_SMOKE_SESSIONS_PER_IDENTITY,
  normalizeLoadSmokeBaseURL,
  writePrivateLoadSmokeFixture,
} from '../fixtures/load-smoke.mjs'
import { writePrivateJsonFixture } from '../fixtures/private-json.mjs'
import { VerificationLifecycleClient } from '../fixtures/lifecycle-client.mjs'
import { PodmanNetworkShapingDriver } from '../fixtures/network-shaping.mjs'
import { assertVerificationRunId } from '../fixtures/run-scope.mjs'
import { grantAndAccept } from '../fixtures/shares.mjs'

const PROFILE = process.env.INQTRIX_LOAD_PROFILE?.trim() || 'load-smoke'
const MEMORY_RECOVERY_BASELINE_PHASE = 'latency-100ms'
if (!['load-smoke', 'load-soak', 'load-ramp'].includes(PROFILE)) {
  throw new Error(
    'Generated load fixture engine supports load-smoke, load-soak, or load-ramp only.',
  )
}

const runId = requiredEnvironment('INQTRIX_VERIFICATION_RUN_ID')
const fixturePath = requiredEnvironment('INQTRIX_LOAD_SESSION_FIXTURE')
const reportDirectory = requiredEnvironment('INQTRIX_VERIFICATION_REPORT_DIR')
const adminEmail = requiredEnvironment('INQTRIX_E2E_ADMIN_EMAIL')
const adminPassword = requiredEnvironment('INQTRIX_E2E_ADMIN_PASSWORD')
const userPassword = requiredEnvironment('INQTRIX_E2E_USER_PASSWORD')
const baseURL = normalizeLoadSmokeBaseURL(
  process.env.INQTRIX_E2E_BASE_URL ?? 'http://127.0.0.1:8080',
)
const documentSeed = buildLoadDocumentSeed({
  loadProfile: PROFILE,
  requestedProfile: process.env.INQTRIX_LOAD_SMOKE_DOCUMENT_PROFILE,
  runId,
})
const ignoreHTTPSErrors = process.env.INQTRIX_E2E_IGNORE_HTTPS_ERRORS === '1'
const repositoryRoot = fileURLToPath(new URL('../../..', import.meta.url))

assertVerificationRunId(runId)

const lifecycle = new VerificationLifecycleClient({
  reportDirectory,
  runId,
})
const sessions = createApiSessionFixtures({
  baseURL,
  ignoreHTTPSErrors,
  lifecycle,
  runId,
})
let activity = null
let admin
let control = null
let document
let networkCleanupHandle = null
let networkDriver = null
let temporaryUsers = []
let temporaryActors = []

try {
  progress('Authenticating the provisioning owner.')
  admin = await sessions.loginActor(
    adminEmail,
    adminPassword,
    `${PROFILE} owner`,
    'admin',
  )
  const capabilities = await fetchActorJson(admin, 'GET', '/v1/capabilities')
  assertFixture(
    capabilities.features?.sharing === true
      && capabilities.features?.collaboration === true
      && capabilities.feature_status?.collaboration?.state === 'enabled',
    `${PROFILE} requires enabled sharing and collaboration capabilities.`,
  )

  const temporaryCount = PROFILE === 'load-soak'
    ? 24
    : PROFILE === 'load-ramp'
      ? RAMP_MAX_IDENTITIES
      : 4
  progress(`Creating ${temporaryCount} Run-ID-bound temporary identities.`)
  temporaryUsers = await ensureTemporaryUsers({
    adminActor: admin,
    descriptors: temporaryUserDescriptors(runId, temporaryCount),
    lifecycle,
    password: userPassword,
    runId,
  })
  for (const user of temporaryUsers) {
    temporaryActors.push(
      await sessions.loginActor(
        user.email,
        userPassword,
        user.displayName,
        'user',
      ),
    )
  }
  assertFixture(
    new Set(temporaryActors.map((actor) => actor.user.id)).size === temporaryCount,
    `${PROFILE} temporary accounts did not resolve to distinct identities.`,
  )

  document = await createCollaborationDocument({
    lifecycle,
    markdown: documentSeed.markdown,
    owner: admin,
    runId,
    schemaVersion: capabilities.collaboration.schema_version,
    title: PROFILE === 'load-soak'
      ? 'Load soak'
      : PROFILE === 'load-ramp'
        ? 'Load ramp'
        : documentSeed.profile === 'large-state'
          ? 'Load smoke large state'
          : 'Load smoke',
  })

  if (PROFILE === 'load-ramp') {
    const summary = await runRamp({ capabilities })
    await writePrivateJsonFixture(
      resolve(reportDirectory, 'load-ramp-result.json'),
      summary,
    )
    await writeScenarioSidecar(
      process.env.INQTRIX_VERIFICATION_SCENARIO_RESULTS_PATH,
      [
        {
          // A budget stop is an honest local ceiling, not a red gate;
          // only an integrity break fails the ladder.
          id: 'load-ramp.ladder',
          status: summary.status === 'failed' ? 'failed' : 'passed',
        },
        {
          id: 'load-ramp.integrity',
          status: summary.stopKind === 'integrity' ? 'failed' : 'passed',
        },
      ],
    )
  } else if (PROFILE === 'load-smoke') {
    const result = await runSmoke({ capabilities })
    await writePrivateJsonFixture(
      resolve(reportDirectory, 'load-smoke-result.json'),
      {
        document: {
          markdownCharacters: documentSeed.characterCount,
          paragraphs: documentSeed.paragraphCount,
          profile: documentSeed.profile,
        },
        result,
        version: 1,
      },
    )
  } else {
    await runSoak({ capabilities })
  }
} finally {
  await activity?.settle().catch(() => undefined)
  let networkClosed = false
  if (control) {
    try {
      await control.close()
      networkClosed = true
    } catch {
      // The parent lifecycle retains the registered qdisc cleanup handle.
    }
    control = null
    networkDriver = null
  } else if (networkDriver) {
    try {
      await networkDriver.close()
      networkClosed = true
    } catch {
      // The parent lifecycle retains the registered qdisc cleanup handle.
    }
    networkDriver = null
  }
  if (networkClosed && networkCleanupHandle) {
    await lifecycle.complete(networkCleanupHandle).catch(() => undefined)
    networkCleanupHandle = null
  }
  if (document && admin) {
    await deleteCollaborationDocument({
      document,
      lifecycle,
      owner: admin,
    }).catch(() => undefined)
  }
  for (const actor of [...temporaryActors].reverse()) {
    await sessions.logoutActor(actor).catch(() => undefined)
  }
  if (admin) {
    if (PROFILE === 'load-smoke') {
      for (const user of [...temporaryUsers].reverse()) {
        await eraseTemporaryUser(admin, user, lifecycle).catch(() => undefined)
      }
    }
    await sessions.logoutActor(admin).catch(() => undefined)
  }
  await sessions.closeAll()
  lifecycle.close()
}

async function runSmoke({ capabilities }) {
  await grantAndAccept({
    document,
    lifecycle,
    owner: admin,
    recipients: temporaryActors.map((actor) => [actor, 'edit']),
  })
  progress('Issuing 20 short-lived collaboration leases.')
  const groups = await Promise.all(
    temporaryActors.map(async (actor) => {
      const issued = []
      for (let index = 0; index < LOAD_SMOKE_SESSIONS_PER_IDENTITY; index += 1) {
        issued.push(await issueCollaborationSession(actor, capabilities))
      }
      return issued
    }),
  )
  const issuedSessions = []
  for (let sessionIndex = 0; sessionIndex < LOAD_SMOKE_SESSIONS_PER_IDENTITY; sessionIndex += 1) {
    for (const group of groups) issuedSessions.push(group[sessionIndex])
  }
  const fixture = buildLoadSmokeFixture({
    baseURL,
    runId,
    sessions: issuedSessions,
  })
  await writePrivateLoadSmokeFixture(fixturePath, fixture)
  progress('Running 20 sockets with five concurrent writers.')
  return await runCollaborationLoad(
    ['--mode', 'smoke', '--fixture', fixturePath, '--json'],
    process.env,
  )
}

/** The LOCAL capacity ramp.
 *
 * Every rung reuses one room and the same capped identity pool, so what
 * grows is socket fan-out — not tenant or identity scale. That limit is
 * carried in the result rather than glossed over, and it is the reason a
 * completed ladder still leaves the production capacity proof open.
 *
 * Two stop kinds, deliberately unequal:
 *   integrity break -> red, the ladder failed;
 *   headroom/budget -> controlled stop at an honest local ceiling.
 * A refused rung is always announced; a silent truncation would read as
 * full coverage.
 */
async function runRamp({ capabilities }) {
  const engine = requiredEnvironment('INQTRIX_E2E_CONTAINER_ENGINE')
  assertFixture(engine === 'podman', 'The load ramp requires Podman for resource gating.')
  // Same container resolution the soak uses — the monitor must refuse any
  // container outside the canonical Compose project.
  const containers = await resolveFaultControlContainers({ engine, repositoryRoot })
  const monitor = new PodmanResourceMonitor({
    project: containers.project,
    repositoryRoot,
  })
  await monitor.initialize()

  await grantAndAccept({
    document,
    lifecycle,
    owner: admin,
    recipients: temporaryActors.map((actor) => [actor, 'edit']),
  })

  // The product caps concurrent sessions per user and document. Obey it:
  // the ladder is cut to what that contract permits, and the rest stays
  // visibly unproven rather than being bought by weakening the guard.
  const { ceiling, reachable, unreachable } = partitionRampStages()
  if (unreachable.length > 0) {
    progress(
      `Product session cap allows ${ceiling} sockets locally `
      + `(${temporaryActors.length} identities x ${RAMP_SESSIONS_PER_IDENTITY}). `
      + `Rungs ${unreachable.join(', ')} are NOT attempted and remain an open `
      + 'production-capacity proof.',
    )
  }
  const stages = planRampStages(reachable)
  const maxSessions = stages.at(-1).connections
  const perIdentity = Math.min(
    RAMP_SESSIONS_PER_IDENTITY,
    Math.ceil(maxSessions / temporaryActors.length),
  )
  progress(
    `Issuing ${maxSessions} leases across ${temporaryActors.length} identities `
    + `(${perIdentity} each) — socket fan-out, not identity scale.`,
  )
  const groups = await Promise.all(
    temporaryActors.map(async (actor) => {
      const issued = []
      for (let index = 0; index < perIdentity; index += 1) {
        issued.push(await issueCollaborationSession(actor, capabilities))
      }
      return issued
    }),
  )
  // Interleave so any prefix of the list spreads across all identities.
  const ordered = []
  for (let index = 0; index < perIdentity; index += 1) {
    for (const group of groups) {
      if (group[index]) ordered.push(group[index])
    }
  }

  const executed = []
  for (const stage of stages) {
    try {
      await monitor.assertHeadroom(`ramp-${stage.connections}-before`)
    } catch (error) {
      progress(
        `Rung ${stage.connections} refused for resource headroom: ${String(error?.message ?? error)}`,
      )
      executed.push({
        connections: stage.connections,
        integrityPassed: true,
        latencyPassed: false,
        reason: `headroom refused rung ${stage.connections}: ${String(error?.message ?? error)}`,
      })
      break
    }
    const fixture = buildLoadRampFixture({
      baseURL,
      identityCeiling: RAMP_MAX_IDENTITIES,
      runId,
      sessions: ordered.slice(0, stage.connections),
      writers: stage.writers,
    })
    await writePrivateLoadSmokeFixture(fixturePath, fixture)
    progress(
      `Rung ${stage.connections}: ${stage.writers} writers, ${stage.observers} observers.`,
    )
    const result = await runCollaborationLoad(
      [
        '--mode', 'ramp',
        '--fixture', fixturePath,
        '--connections', String(stage.connections),
        '--writers', String(stage.writers),
        '--observers', String(stage.observers),
        '--json',
      ],
      process.env,
    )
    const integrityPassed = result?.reconstruction?.passed === true
    const latencyPassed = result?.gates?.visibleUpdatePassed === true
      && result?.gates?.durableAckPassed === true
    executed.push({
      connections: stage.connections,
      durableAckP95Ms: result?.durableAckMs?.p95 ?? null,
      integrityPassed,
      latencyPassed,
      reason: integrityPassed
        ? (latencyPassed ? null : `latency budget exceeded at ${stage.connections} sockets`)
        : `reconstruction failed at ${stage.connections} sockets`,
      visibleUpdateP95Ms: result?.visibleUpdateMs?.p95 ?? null,
    })
    if (!integrityPassed || !latencyPassed) break
    await monitor.capture(`ramp-${stage.connections}-after`)
  }

  const summary = summarizeRamp(executed, reachable)
  progress(
    `Ramp ${summary.status}: highest reached ${summary.highestPassedConnections} sockets; `
    + `unreached within cap ${summary.unreachedConnections.join(', ') || 'none'}; `
    + `blocked by product session cap ${unreachable.join(', ') || 'none'}; `
    + 'production capacity proof remains OUTSTANDING.',
  )
  return {
    ...summary,
    identities: temporaryActors.length,
    sessionsPerIdentity: perIdentity,
    socketCeiling: ceiling,
    stages: executed,
    stagesBlockedByProductCap: unreachable,
  }
}

async function runSoak({ capabilities }) {
  const engine = requiredEnvironment('INQTRIX_E2E_CONTAINER_ENGINE')
  assertFixture(engine === 'podman', 'Load-soak network shaping requires Podman.')
  const writers = [admin, ...temporaryActors.slice(0, 4)]
  const commenters = temporaryActors.slice(4, 9)
  const readers = temporaryActors.slice(9, 19)
  const featureActors = temporaryActors.slice(19, 24)
  const orderedActors = [...writers, ...commenters, ...readers, ...featureActors]
  assertFixture(
    orderedActors.length === 25
      && new Set(orderedActors.map((actor) => actor.user.id)).size === 25,
    'Load-soak requires 25 distinct ordered identities.',
  )

  await grantAndAccept({
    document,
    lifecycle,
    owner: admin,
    recipients: [
      ...temporaryActors.slice(0, 4).map((actor) => [actor, 'edit']),
      ...commenters.map((actor) => [actor, 'suggest']),
      ...readers.map((actor) => [actor, 'view']),
      ...featureActors.map((actor) => [actor, 'view']),
    ],
  })

  progress('Issuing one collaboration lease for each of 25 identities.')
  const issuedSessions = []
  for (const actor of orderedActors) {
    issuedSessions.push(await issueCollaborationSession(actor, capabilities))
  }

  progress('Resolving exact canonical containers and measuring resource headroom.')
  const containers = await resolveFaultControlContainers({
    engine,
    repositoryRoot,
  })
  const resourceMonitor = new PodmanResourceMonitor({
    project: containers.project,
    repositoryRoot,
  })
  const baselineResources = await resourceMonitor.initialize()
  let memoryRecoveryBaseline = null
  networkDriver = new PodmanNetworkShapingDriver({
    containerId: containers.collaboration,
    peerContainerId: containers.web,
    repositoryRoot,
  })
  await networkDriver.initialize()
  networkCleanupHandle = await lifecycle.register({
    composeProject: containers.project,
    containerId: containers.collaboration,
    engine,
    id: `${runId}:network-qdisc:${containers.collaboration}`,
    kind: 'network_qdisc',
  })

  activity = new LoadSoakProductActivity({
    commenters,
    document,
    featureActors,
    lifecycle,
    moderator: admin,
    readers,
    runId,
    writers,
  })
  await activity.initialize()

  const sessionReissuer = createRegisteredSessionReissuer({
    async issueSession({ actor, currentSession, rotationCommandId }) {
      return await issueCollaborationSession(actor, capabilities, {
        currentLeaseToken: currentSession.lease_token,
        rotationCommandId,
      })
    },
  })
  const token = randomBytes(32).toString('base64url')
  control = await startLoadControlServer({
    beforeNetworkPhase: async (phaseId) => {
      const snapshot = await resourceMonitor.assertHeadroom(`before-${phaseId}`)
      if (phaseId === MEMORY_RECOVERY_BASELINE_PHASE) {
        if (memoryRecoveryBaseline) {
          throw new Error('Load-soak memory recovery baseline was already recorded.')
        }
        memoryRecoveryBaseline = snapshot
      }
    },
    networkDriver,
    onNetworkPhase: (phaseId) => activity.onNetworkPhase(phaseId),
    reissueSession: sessionReissuer.reissueSession,
    runId,
    token,
  })
  const authorizationEnv = 'INQTRIX_LOAD_CONTROL_TOKEN'
  const fixture = buildLoadSoakFixture({
    baseURL,
    controls: {
      authorizationEnv,
      baseURL: control.baseURL,
      networkPath: control.paths.networkPhase,
      reissuePath: control.paths.sessionReissue,
    },
    runId,
    sessions: issuedSessions,
  })
  fixture.sessions.forEach((session, index) => {
    sessionReissuer.register(
      session.reissue_id,
      orderedActors[index],
      issuedSessions[index],
    )
  })
  await writePrivateLoadSmokeFixture(fixturePath, fixture)

  progress('Running the resource-guarded 30-minute mixed product soak.')
  const loadEnvironment = {
    ...process.env,
    [authorizationEnv]: token,
  }
  delete loadEnvironment.INQTRIX_VERIFICATION_SCENARIO_RESULTS_PATH
  await runAndFinalizeLoadSoakEvidence({
    captureResourceRecovery: async () => {
      progress('Waiting 30 seconds for post-load resource recovery.')
      await delay(30_000)
      const final = await resourceMonitor.capture('post-quiet')
      if (!memoryRecoveryBaseline) {
        throw new Error(
          'Load-soak memory recovery requires the completed initial normal phase.',
        )
      }
      return {
        final,
        recovery: resourceMonitor.recovery(final, {
          memoryBaseline: memoryRecoveryBaseline,
        }),
        snapshots: resourceMonitor.snapshots,
      }
    },
    finishProductActivity: async () => await activity.finish(),
    runCollaboration: async () => await runCollaborationLoad(
      ['--mode', 'soak', '--fixture', fixturePath, '--json'],
      loadEnvironment,
    ),
    writeResourceEvidence: async (evidence) => {
      await writeFile(
        resolve(reportDirectory, 'load-soak-resources.json'),
        `${JSON.stringify({
          axes: evidence.axes,
          baseline: baselineResources,
          collaboration: evidence.collaboration,
          final: evidence.resources?.final ?? null,
          productActivity: evidence.productActivity,
          recovery: evidence.resources?.recovery ?? null,
          snapshots: evidence.resources?.snapshots ?? resourceMonitor.snapshots,
        }, null, 2)}\n`,
        { encoding: 'utf8', mode: 0o600 },
      )
    },
    writeScenarioEvidence: async ({ collaboration, supplemental }) => {
      await writeScenarioResults(
        'soak',
        collaboration?.gates ?? unavailableSoakGates(),
        collaboration?.reconstruction ?? { passed: false },
        collaboration?.sessionRotation ?? { passed: false },
        process.env,
        supplemental,
      )
    },
  })
}

function unavailableSoakGates() {
  return {
    apiLatencyStatus: 'failed',
    apiSampleSpanPassed: false,
    durableAckPassed: false,
    minimumAckRoundsPassed: false,
    minimumDurationPassed: false,
    observerCohortPassed: false,
    phaseResultsPassed: false,
    visibleUpdatePassed: false,
  }
}

async function issueCollaborationSession(
  actor,
  capabilities,
  { currentLeaseToken = null, rotationCommandId = null } = {},
) {
  if ((currentLeaseToken === null) !== (rotationCommandId === null)) {
    throw new Error('Collaboration lease rotation requires both current lease and command ID.')
  }
  const data = {
    protocol_version: capabilities.collaboration.protocol_version,
    schema_version: capabilities.collaboration.schema_version,
  }
  if (currentLeaseToken !== null) {
    data.lease_token = currentLeaseToken
    data.rotation_command_id = rotationCommandId
  }
  return await fetchActorJson(
    actor,
    'POST',
    `/v1/editor/documents/${document.id}/collaboration/session`,
    {
      data,
    },
  )
}

function requiredEnvironment(name) {
  const value = process.env[name]?.trim()
  if (!value) throw new Error(`${name} is required.`)
  return value
}

function progress(message) {
  process.stderr.write(`[${PROFILE}] ${message}\n`)
}
