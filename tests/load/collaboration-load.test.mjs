import assert from 'node:assert/strict'
import {
  ACCEPTED_LATENCY_CEILING_P95_MS,
  RAMP_MAX_IDENTITIES,
  RAMP_SESSIONS_PER_IDENTITY,
  RAMP_STAGES,
  partitionRampStages,
  planRampStages,
  rampSocketCeiling,
  summarizeRamp,
} from './collaboration-load-lib.mjs'
import { spawnSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import {
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { describe, test } from 'node:test'
import { fileURLToPath } from 'node:url'

import * as Y from 'yjs'

import {
  formatApiProbe,
  formatVisibleUpdateGate,
  writeScenarioResults,
} from './collaboration-load.mjs'
import {
  API_DEGRADATION_LIMIT_PERCENT,
  API_P95_LIMIT_MS,
  API_PROBE_CONTRACT,
  API_RELATIVE_GATE_MIN_BASELINE_MS,
  AUTH_DENIED,
  AUTH_TOKEN,
  AUTHENTICATED,
  ByteDecoder,
  FatalSocketState,
  INSTANCE_PROBE_CONTRACT,
  INSTANCE_PROBE_PATH,
  MESSAGE_AUTH,
  MESSAGE_CLOSE,
  MESSAGE_STATELESS,
  MESSAGE_SYNC,
  MESSAGE_SYNC_REPLY,
  CAPACITY_CONNECTIONS,
  CAPACITY_DURABLE_P95_MS,
  CAPACITY_LEASE_TTL_SECONDS,
  CAPACITY_MIN_ACK_ROUNDS_PER_WRITER,
  CAPACITY_MIN_DURATION_MS,
  CAPACITY_OBSERVER_COHORT,
  CAPACITY_VISIBLE_P95_MS,
  CAPACITY_WRITERS,
  LOAD_NETWORK_CONTROL_CONTRACT,
  RawCollaborationClient,
  SOAK_CONNECTIONS,
  SOAK_MIN_DURATION_MS,
  SOAK_NETWORK_PHASES,
  SOAK_OBSERVER_COHORT,
  SOAK_PHASE_DURATION_MS,
  SOAK_WRITER_INTERVAL_MS,
  SOAK_WRITERS,
  SMOKE_VISIBLE_WARNING_P95_MS,
  SESSION_REISSUE_CONTRACT,
  SessionRotationSupervisor,
  allLoadGatesPassed,
  applyNetworkPhase,
  SYNC_STEP_ONE,
  SYNC_STEP_TWO,
  assertCapacityPreflight,
  assertSoakPreflight,
  concatBytes,
  encodeBytes,
  encodeRoutedFrame,
  encodeString,
  encodeVarUint,
  evaluateGates,
  loadFixture,
  measureApiProbe,
  observeCollaborationInstance,
  parseArguments,
  parseInstanceProbePayload,
  parseRestartAcknowledgement,
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
  summarizeApiProbe,
  verifyObserverCohort,
  verifyReconstructedMarkers,
} from './collaboration-load-lib.mjs'

describe('capacity load options', () => {
  test('capacity mode pins every architecture capacity and latency value', () => {
    const options = parseArguments(['--mode', 'capacity'])

    assert.equal(options.connections, CAPACITY_CONNECTIONS)
    assert.equal(options.writers, CAPACITY_WRITERS)
    assert.equal(options.observers, CAPACITY_OBSERVER_COHORT)
    assert.equal(options.minDurationMs, CAPACITY_MIN_DURATION_MS)
    assert.equal(options.minAckRoundsPerWriter, CAPACITY_MIN_ACK_ROUNDS_PER_WRITER)
    assert.equal(options.visibleUpdateP95Ms, CAPACITY_VISIBLE_P95_MS)
    assert.equal(options.durableAckP95Ms, CAPACITY_DURABLE_P95_MS)
  })

  for (const args of [
    ['--mode', 'capacity', '--connections', '999'],
    ['--mode', 'capacity', '--writers', '99'],
    ['--mode', 'capacity', '--observers', '1'],
    ['--mode', 'capacity', '--min-duration-ms', '1'],
    ['--mode', 'capacity', '--min-ack-rounds', '1'],
    ['--mode', 'capacity', '--visible-p95-ms', '251'],
    ['--mode', 'capacity', '--durable-p95-ms', '501'],
    ['--mode', 'capacity', '--post-sample-quiet-ms', '1'],
    ['--mode', 'capacity', '--allow-insecure-tls'],
    ['--mode', 'capacity', '--skip-api-probe'],
  ]) {
    test(`capacity mode rejects ${args.slice(2).join(' ')}`, () => {
      assert.throws(() => parseArguments(args), /capacity|Capacity/)
    })
  }

  test('smoke mode is explicitly parameterizable and labelled smoke', () => {
    const options = parseArguments([
      '--mode', 'smoke',
      '--connections', '7',
      '--writers', '2',
      '--observers', '2',
      '--min-duration-ms', '20',
      '--min-ack-rounds', '3',
      '--visible-p95-ms', '900',
      '--durable-p95-ms', '1200',
      '--skip-api-probe',
    ])

    assert.equal(options.mode, 'smoke')
    assert.equal(options.connections, 7)
    assert.equal(options.writers, 2)
    assert.equal(options.observers, 2)
    assert.equal(options.minDurationMs, 20)
    assert.equal(options.minAckRoundsPerWriter, 3)
    assert.equal(options.skipApiProbe, true)
  })

  test('soak mode fixes 25 identities, 30 minutes, pacing, and six network phases', () => {
    const options = parseArguments(['--mode', 'soak'])

    assert.equal(options.mode, 'soak')
    assert.equal(options.connections, SOAK_CONNECTIONS)
    assert.equal(options.writers, SOAK_WRITERS)
    assert.equal(options.observers, SOAK_OBSERVER_COHORT)
    assert.equal(options.minDurationMs, SOAK_MIN_DURATION_MS)
    assert.equal(options.writerIntervalMs, SOAK_WRITER_INTERVAL_MS)
    assert.equal(SOAK_NETWORK_PHASES.length, 6)
    assert.equal(SOAK_NETWORK_PHASES[0].id, 'normal')
    assert.equal(SOAK_NETWORK_PHASES.at(-1).id, 'normalized')
    assert(SOAK_NETWORK_PHASES.every((phase) => phase.durationMs === SOAK_PHASE_DURATION_MS))
    assert.equal(
      SOAK_NETWORK_PHASES.reduce((total, phase) => total + phase.durationMs, 0),
      SOAK_MIN_DURATION_MS,
    )
  })

  for (const args of [
    ['--mode', 'soak', '--connections', '24'],
    ['--mode', 'soak', '--writers', '4'],
    ['--mode', 'soak', '--observers', '9'],
    ['--mode', 'soak', '--min-duration-ms', '1000'],
    ['--mode', 'soak', '--visible-p95-ms', '999'],
    ['--mode', 'soak', '--skip-api-probe'],
  ]) {
    test(`soak mode rejects ${args.slice(2).join(' ')}`, () => {
      assert.throws(() => parseArguments(args), /soak|Soak/)
    })
  }

  test('capacity help fails before fixture access while smoke help remains available', () => {
    assert.throws(
      () => parseArguments(['--mode', 'capacity', '--help']),
      /Capacity mode forbids --help/,
    )
    assert.equal(parseArguments(['--mode', 'smoke', '--help']).help, true)

    const runner = fileURLToPath(new URL('./collaboration-load.mjs', import.meta.url))
    const capacity = spawnSync(
      process.execPath,
      [runner, '--mode', 'capacity', '--help'],
      { encoding: 'utf8', env: {} },
    )
    assert.equal(capacity.status, 1)
    assert.match(capacity.stderr, /Capacity mode forbids --help/)
    assert.doesNotMatch(capacity.stderr, /lease\/session fixture/)
    assert.equal(capacity.stdout, '')

    const smoke = spawnSync(
      process.execPath,
      [runner, '--mode', 'smoke', '--help'],
      { encoding: 'utf8', env: {} },
    )
    assert.equal(smoke.status, 0)
    assert.match(smoke.stdout, /npm run verify:load-smoke/)
  })
})

describe('capacity fixture preflight', () => {
  test('requires secure same-origin probes, WebSocket path, Origin, and restart control', () => {
    const options = parseArguments(['--mode', 'capacity'])
    const sessions = [session({ websocketUrl: 'wss://collaboration.example.test/collaboration' })]
    const control = {
      authorization: 'Bearer test-control-value',
      url: new URL('https://control.example.test/restart'),
    }

    assert.doesNotThrow(() => assertCapacityPreflight(
      options,
      sessions,
      apiProbe(),
      control,
      instanceProbe(),
      sessionReissueControl(),
    ))
    assert.throws(
      () => assertCapacityPreflight(
        options,
        sessions,
        apiProbe('https://collaboration.example.test:444/health'),
        control,
        instanceProbe(`https://collaboration.example.test:444${INSTANCE_PROBE_PATH}`),
        sessionReissueControl(),
      ),
      /same origin including effective port/,
    )
    assert.throws(
      () => assertCapacityPreflight(
        options,
        sessions,
        apiProbe(),
        null,
        instanceProbe(),
        sessionReissueControl(),
      ),
      /restart_control/,
    )
    assert.throws(
      () => assertCapacityPreflight(
        options,
        sessions,
        apiProbe(),
        { ...control, url: new URL('http://control.example.test/restart') },
        instanceProbe(),
        sessionReissueControl(),
      ),
      /restart control must use HTTPS/,
    )
    assert.throws(
      () => assertCapacityPreflight(
        options,
        sessions,
        apiProbe(),
        control,
        null,
        sessionReissueControl(),
      ),
      /requires fixture.instance_probe/,
    )
    assert.throws(
      () => assertCapacityPreflight(
        options,
        sessions,
        apiProbe(),
        control,
        instanceProbe(`https://other.example.test${INSTANCE_PROBE_PATH}`),
        sessionReissueControl(),
      ),
      /public API\/WebSocket origin/,
    )
    assert.throws(
      () => assertCapacityPreflight(
        options,
        sessions,
        apiProbe(),
        control,
        instanceProbe(),
        null,
      ),
      /requires fixture.session_reissue/,
    )
    assert.throws(
      () => assertCapacityPreflight(
        options,
        sessions,
        apiProbe(),
        control,
        instanceProbe(),
        sessionReissueControl({ url: new URL('http://control.example.test/reissue') }),
      ),
      /session reissue must use HTTPS/,
    )
    assert.throws(
      () => assertCapacityPreflight(
        options,
        sessions,
        apiProbe(),
        control,
        instanceProbe(),
        sessionReissueControl({ leaseTtlSeconds: 120 }),
      ),
      /60-second leases/,
    )
  })

  for (const [name, sessions, probe, pattern] of [
    [
      'cleartext API',
      [session()],
      apiProbe('http://collaboration.example.test/health'),
      /HTTPS.*\/health/,
    ],
    [
      'cleartext WebSocket',
      [session({ websocketUrl: 'ws://collaboration.example.test/collaboration' })],
      apiProbe(),
      /WSS.*\/collaboration/,
    ],
    [
      'arbitrary WebSocket path',
      [session({ websocketUrl: 'wss://collaboration.example.test/socket' })],
      apiProbe(),
      /exact public \/collaboration path/,
    ],
    [
      'SPA/API alias path',
      [session()],
      apiProbe('https://collaboration.example.test/app'),
      /exact FastAPI \/health path/,
    ],
    [
      'mismatched Origin header',
      [session({ origin: 'https://other.example.test' })],
      apiProbe(),
      /Origin header must exactly match/,
    ],
  ]) {
    test(`rejects ${name}`, () => {
      assert.throws(
        () => assertCapacityPreflight(
          parseArguments(['--mode', 'capacity']),
          sessions,
          probe,
          restartControl(),
          instanceProbe(probe.url.protocol === 'http:'
            ? `http://collaboration.example.test${INSTANCE_PROBE_PATH}`
            : undefined),
          sessionReissueControl(),
        ),
        pattern,
      )
    })
  }

  test('resolves only the declared FastAPI health probe contract', () => {
    const fixture = {
      api_probe: { contract: API_PROBE_CONTRACT, url: '/health' },
      base_url: 'https://collaboration.example.test',
    }
    const probe = resolveApiProbe(fixture, parseArguments(['--mode', 'capacity']))

    assert.equal(probe.contract, API_PROBE_CONTRACT)
    assert.equal(probe.url.toString(), 'https://collaboration.example.test/health')
    assert.throws(
      () => resolveApiProbe(
        { api_probe_url: '/health', base_url: fixture.base_url },
        parseArguments(['--mode', 'capacity']),
      ),
      /requires fixture.api_probe/,
    )
  })

  test('requires a declared production instance probe contract', () => {
    const fixture = {
      base_url: 'https://collaboration.example.test',
      instance_probe: {
        contract: INSTANCE_PROBE_CONTRACT,
        url: INSTANCE_PROBE_PATH,
      },
    }
    const probe = resolveInstanceProbe(fixture, parseArguments(['--mode', 'capacity']))

    assert.equal(probe.contract, INSTANCE_PROBE_CONTRACT)
    assert.equal(
      probe.url.toString(),
      `https://collaboration.example.test${INSTANCE_PROBE_PATH}`,
    )
    assert.throws(
      () => resolveInstanceProbe(
        { base_url: fixture.base_url },
        parseArguments(['--mode', 'capacity']),
      ),
      /requires fixture.instance_probe/,
    )
    assert.throws(
      () => resolveInstanceProbe(
        {
          ...fixture,
          instance_probe: { contract: 'controller-assertion', url: '/instance' },
        },
        parseArguments(['--mode', 'capacity']),
      ),
      /must equal inqtrix-collaboration-instance-v1/,
    )
    assert.throws(
      () => assertCapacityPreflight(
        parseArguments(['--mode', 'capacity']),
        [session()],
        apiProbe(),
        restartControl(),
        instanceProbe('https://collaboration.example.test/controller-instance'),
        sessionReissueControl(),
      ),
      /at \/collaboration\/instance/,
    )
  })

  test('parses session fields without exposing a rejected lease token', () => {
    const sentinel = 'never-print-this-lease-token'
    const fixture = {
      base_url: 'https://collaboration.example.test',
      sessions: [{
        access: 'edit',
        expires_at: 1,
        initial_write_mode: 'edit',
        lease_token: sentinel,
        protocol_version: 1,
        refresh_after: 0.5,
        reissue_id: 'fixture-session-1',
        room: 'inqtrix-editor-v1:document:g1',
        schema_version: 1,
        user: { id: 'user-1' },
        websocket_path: '/collaboration',
      }],
      version: 2,
    }
    const directory = mkdtempSync(join(tmpdir(), 'inqtrix-load-fixture-'))
    const fixturePath = join(directory, 'session.json')
    writeFileSync(fixturePath, JSON.stringify(fixture))
    const parsed = loadFixture(fixturePath)

    assert.throws(
      () => prepareSessions(parsed, { connections: 1 }, 100),
      (error) => {
        assert(error instanceof Error)
        assert.doesNotMatch(error.message, new RegExp(sentinel))
        assert.match(error.message, /expires_at/)
        return true
      },
    )
  })

  test('rejects a room outside the editor collaboration contract before networking', () => {
    const fixture = {
      base_url: 'https://collaboration.example.test',
      sessions: [{
        access: 'edit',
        expires_at: 1_000,
        initial_write_mode: 'edit',
        lease_token: 'test-only-lease-value',
        protocol_version: 1,
        refresh_after: 900,
        reissue_id: 'fixture-session-1',
        room: 'unexpected-room',
        schema_version: 1,
        user: { id: 'user-1' },
        websocket_path: '/collaboration',
      }],
      version: 2,
    }

    assert.throws(
      () => prepareSessions(fixture, { connections: 1 }, 100),
      /editor collaboration room contract/,
    )
  })

  test('restart control keeps authorization in memory and validates its fixture contract', () => {
    const sentinel = 'never-print-this-restart-token'
    const fixture = {
      restart_control: {
        authorization_env: 'INQTRIX_LOAD_RESTART_TOKEN',
        base_url: 'https://control.example.test',
        restart_path: '/v1/test/collaboration/restart',
      },
    }
    const control = resolveRestartControl(
      fixture,
      { mode: 'capacity' },
      { INQTRIX_LOAD_RESTART_TOKEN: sentinel },
    )

    assert.equal(control.authorization, `Bearer ${sentinel}`)
    assert.equal(control.url.toString(), 'https://control.example.test/v1/test/collaboration/restart')
  })

  test('session reissue control requires fixture v2, HTTPS capacity metadata, and in-memory authorization', () => {
    const sentinel = 'never-print-this-reissue-token'
    const fixture = {
      session_reissue: {
        authorization_env: 'INQTRIX_LOAD_REISSUE_TOKEN',
        contract: SESSION_REISSUE_CONTRACT,
        lease_ttl_seconds: CAPACITY_LEASE_TTL_SECONDS,
        url: 'https://control.example.test/v1/test/collaboration/sessions/reissue',
      },
    }
    const control = resolveSessionReissueControl(
      fixture,
      { mode: 'capacity' },
      { INQTRIX_LOAD_REISSUE_TOKEN: sentinel },
    )

    assert.equal(control.authorization, `Bearer ${sentinel}`)
    assert.equal(control.contract, SESSION_REISSUE_CONTRACT)
    assert.equal(control.leaseTtlSeconds, 60)
    assert.throws(
      () => resolveSessionReissueControl(fixture, { mode: 'capacity' }, {}),
      (error) => {
        assert(error instanceof Error)
        assert.doesNotMatch(error.message, new RegExp(sentinel))
        assert.match(error.message, /INQTRIX_LOAD_REISSUE_TOKEN/)
        return true
      },
    )

    const directory = mkdtempSync(join(tmpdir(), 'inqtrix-load-fixture-v1-'))
    const fixturePath = join(directory, 'session.json')
    writeFileSync(fixturePath, JSON.stringify({ sessions: [], version: 1 }))
    assert.throws(() => loadFixture(fixturePath), /version=2/)
  })

  test('soak network control is loopback-only, run-scoped, and keeps authorization in memory', async () => {
    const runId = 'inqv-load-network-control-01'
    const sentinel = 'never-print-this-network-token'
    const fixture = {
      network_control: {
        authorization_env: 'INQTRIX_LOAD_CONTROL_TOKEN',
        contract: LOAD_NETWORK_CONTROL_CONTRACT,
        run_id: runId,
        url: 'http://127.0.0.1:43123/control/network-phase',
      },
    }
    const control = resolveNetworkControl(
      fixture,
      { mode: 'soak' },
      { INQTRIX_LOAD_CONTROL_TOKEN: sentinel },
    )
    assert.equal(control.authorization, `Bearer ${sentinel}`)
    assert.equal(control.runId, runId)
    assert.equal(control.url.hostname, '127.0.0.1')

    let request = null
    await applyNetworkPhase(control, 'latency-100ms', async (url, init) => {
      request = { body: JSON.parse(String(init.body)), headers: init.headers, url }
      return new Response(JSON.stringify({
        contract: LOAD_NETWORK_CONTROL_CONTRACT,
        phase_id: 'latency-100ms',
        state: 'applied',
      }), {
        headers: { 'Content-Type': 'application/json' },
        status: 200,
      })
    })
    assert.equal(request.url.toString(), fixture.network_control.url)
    assert.equal(request.headers['X-Inqtrix-Verification-Run-Id'], runId)
    assert.equal(request.body.contract, LOAD_NETWORK_CONTROL_CONTRACT)
    assert.deepEqual(Object.keys(request.body).sort(), ['contract', 'phase_id'])
    assert.throws(
      () => resolveNetworkControl(
        { network_control: { ...fixture.network_control, url: 'http://192.0.2.10/control' } },
        { mode: 'soak' },
        { INQTRIX_LOAD_CONTROL_TOKEN: sentinel },
      ),
      /loopback/,
    )
  })

  test('soak preflight requires 25 ordered identities and one shared control scope', () => {
    const options = parseArguments(['--mode', 'soak'])
    const runId = 'inqv-load-soak-preflight-01'
    const sessions = Array.from({ length: 25 }, (_, index) => session({
      access: index < 5 ? 'edit' : index < 10 ? 'suggest' : 'view',
      reissueId: `soak-session-${index}`,
      userId: `soak-user-${index}`,
    }))
    assert.doesNotThrow(() => assertSoakPreflight(
      options,
      sessions,
      apiProbe(),
      {
        authorization: 'Bearer network',
        contract: LOAD_NETWORK_CONTROL_CONTRACT,
        runId,
        url: new URL('http://127.0.0.1:43123/network'),
      },
      sessionReissueControl({ runId }),
    ))
    assert.throws(
      () => assertSoakPreflight(
        options,
        sessions,
        apiProbe(),
        {
          authorization: 'Bearer network',
          contract: LOAD_NETWORK_CONTROL_CONTRACT,
          runId,
          url: new URL('http://127.0.0.1:43123/network'),
        },
        sessionReissueControl({ runId: 'inqv-load-soak-other-01' }),
      ),
      /share one explicit verification Run ID/,
    )
  })

  test('session reissue sends no lease token and accepts only fresh authenticated API sessions', async () => {
    const now = 2_000_000_000
    const before = session({
      expiresAt: now + 30,
      leaseToken: 'never-send-this-current-lease',
      refreshAfter: now + 20,
    })
    let requestBody = null
    const replacements = await reissueSessions(
      sessionReissueControl(),
      [before],
      'connected_rotation',
      async (url, init) => {
        assert.equal(url.toString(), 'https://control.example.test/reissue')
        assert.equal(init.headers.Authorization, 'Bearer test-reissue-value')
        assert.equal(
          init.headers['X-Inqtrix-Verification-Run-Id'],
          'inqv-load-reissue-test-01',
        )
        assert.doesNotMatch(String(init.body), /never-send-this-current-lease/)
        requestBody = JSON.parse(String(init.body))
        return reissueResponse(before, requestBody.sessions[0].rotation_command_id, {
          expiresAt: now + 60,
          leaseToken: 'new-test-only-lease',
          refreshAfter: now + 45,
        })
      },
      () => now,
    )

    assert.equal(requestBody.contract, SESSION_REISSUE_CONTRACT)
    assert.equal(requestBody.lease_ttl_seconds, 60)
    assert.equal(requestBody.purpose, 'connected_rotation')
    assert.deepEqual(Object.keys(requestBody.sessions[0]).sort(), [
      'reissue_id',
      'rotation_command_id',
    ])
    assert.equal(replacements.length, 1)
    assert.equal(replacements[0].leaseToken, 'new-test-only-lease')
    assert.equal(replacements[0].refreshAfter, now + 45)
    assert.equal(replacements[0].userId, before.userId)

    const freshObservers = await reissueSessions(
      sessionReissueControl(),
      replacements,
      'post_restart_observer',
      async (_url, init) => {
        const request = JSON.parse(String(init.body))
        assert.equal(request.purpose, 'post_restart_observer')
        assert.doesNotMatch(String(init.body), /new-test-only-lease/)
        return reissueResponse(
          replacements[0],
          request.sessions[0].rotation_command_id,
          {
            expiresAt: now + 60,
            leaseToken: 'post-restart-observer-lease',
            refreshAfter: now + 45,
          },
        )
      },
      () => now,
    )
    assert.equal(freshObservers[0].leaseToken, 'post-restart-observer-lease')
  })

  test('session reissue rejects stale, unchanged, or identity-changing responses without leaking tokens', async () => {
    const now = 2_000_000_000
    const sentinel = 'never-print-this-response-lease'
    const before = session({ expiresAt: now + 30, refreshAfter: now + 20 })
    for (const overrides of [
      { expiresAt: now + 10, leaseToken: sentinel, refreshAfter: now + 5 },
      { expiresAt: now + 60, leaseToken: before.leaseToken, refreshAfter: now + 45 },
      { expiresAt: now + 60, leaseToken: sentinel, refreshAfter: now + 45, userId: 'other-user' },
    ]) {
      await assert.rejects(
        () => reissueSessions(
          sessionReissueControl(),
          [before],
          'connected_rotation',
          async (_url, init) => {
            const request = JSON.parse(String(init.body))
            return reissueResponse(before, request.sessions[0].rotation_command_id, overrides)
          },
          () => now,
        ),
        (error) => {
          assert(error instanceof Error)
          assert.doesNotMatch(error.message, new RegExp(sentinel))
          return true
        },
      )
    }
  })

  test('post-restart observer reissue rejects an old lease expiring while the response is in flight', async () => {
    const startedAt = 2_000_000_000
    let now = startedAt
    const before = session({
      expiresAt: startedAt + 1,
      refreshAfter: startedAt + 0.5,
    })
    await assert.rejects(
      () => reissueSessions(
        sessionReissueControl(),
        [before],
        'post_restart_observer',
        async (_url, init) => {
          now = startedAt + 1
          const request = JSON.parse(String(init.body))
          return reissueResponse(before, request.sessions[0].rotation_command_id, {
            expiresAt: now + 60,
            leaseToken: 'unused-post-restart-lease',
            refreshAfter: now + 45,
          })
        },
        () => now,
      ),
      /lease expired after the session reissue response/,
    )
  })

  test('health probe rejects SPA HTML and JSON that is not the FastAPI health schema', async () => {
    await assert.rejects(
      () => measureApiProbe(apiProbe(), async () => new Response('<html>SPA</html>', {
        headers: { 'Content-Type': 'text/html' },
        status: 200,
      })),
      /did not return application\/json/,
    )
    await assert.rejects(
      () => measureApiProbe(apiProbe(), async () => new Response(JSON.stringify({ status: 'ok' }), {
        headers: { 'Content-Type': 'application/json' },
        status: 200,
      })),
      /did not match the Inqtrix FastAPI \/health schema/,
    )
  })

  test('health probe validates every sample against the FastAPI schema', async () => {
    let calls = 0
    const sampleStartedAt = []
    const measurement = await measureApiProbe(apiProbe(), async (url) => {
      calls += 1
      sampleStartedAt.push(performance.now())
      assert.equal(url.toString(), 'https://collaboration.example.test/health')
      return new Response(JSON.stringify(healthPayload()), {
        headers: { 'Content-Type': 'application/json; charset=utf-8' },
        status: 200,
      })
    }, 40)

    assert.equal(calls, 20)
    assert.equal(measurement.latencies.length, 20)
    assert.ok(measurement.sampleSpanMs >= 40)
    assert.ok(sampleStartedAt[10] - sampleStartedAt[0] >= 18)
    assert.ok(sampleStartedAt.at(-1) - sampleStartedAt[0] >= 40)
  })

  test('restart acknowledgement cannot self-attest instance identity', () => {
    assert.deepEqual(parseRestartAcknowledgement(restartPayload()), {
      restartKind: 'ungraceful_process',
      state: 'ready',
    })
    assert.throws(
      () => parseRestartAcknowledgement({ ...restartPayload(), restart_kind: 'graceful' }),
      /ungraceful process restart/,
    )
  })

  test('production instance probe validates identity independently of restart control', async () => {
    const payload = instancePayload({ epoch: 17, instanceId: 'sidecar-production' })
    assert.deepEqual(parseInstanceProbePayload(payload), {
      epoch: 17,
      instanceId: 'sidecar-production',
    })
    assert.throws(
      () => parseInstanceProbePayload({ ...payload, service: 'fixture-controller' }),
      /production data-plane contract/,
    )
    const observed = await observeCollaborationInstance(
      instanceProbe(),
      async () => instanceResponse(payload),
    )
    assert.deepEqual(observed, { epoch: 17, instanceId: 'sidecar-production' })
    await assert.rejects(
      () => observeCollaborationInstance(
        instanceProbe(),
        async () => new Response(JSON.stringify(payload), {
          headers: { 'Content-Type': 'application/json' },
          status: 200,
        }),
      ),
      /Cache-Control: no-store/,
    )
  })

  test('restart is requested while every original socket is still armed and open', async () => {
    const events = []
    let instanceCalls = 0
    const clients = [0, 1].map((index) => ({
      cancelUngracefulRestartExpectation: () => events.push(`cancel-${index}`),
      expectUngracefulRestart: () => events.push(`arm-${index}`),
      waitForUngracefulRestartClose: async () => {
        events.push(`closed-${index}`)
        return 1006
      },
    }))
    const result = await performUngracefulRestart(
      restartControl(),
      'room-1',
      clients,
      1_000,
      instanceProbe(),
      async (url, init) => {
        if (new URL(url).pathname === INSTANCE_PROBE_PATH) {
          instanceCalls += 1
          events.push(instanceCalls === 1 ? 'probe-before' : 'probe-after')
          return instanceResponse(instancePayload({
            epoch: instanceCalls === 1 ? 11 : 12,
            instanceId: instanceCalls === 1 ? 'sidecar-before' : 'sidecar-after',
          }))
        }
        events.push('control')
        assert.deepEqual(events, ['probe-before', 'arm-0', 'arm-1', 'control'])
        assert.deepEqual(JSON.parse(String(init.body)), { room: 'room-1' })
        return new Response(JSON.stringify(restartPayload()), {
          headers: { 'Content-Type': 'application/json' },
          status: 200,
        })
      },
    )

    assert.equal(result.closedSockets, 2)
    assert.deepEqual(result.transition, {
      after: { epoch: 12, instanceId: 'sidecar-after' },
      before: { epoch: 11, instanceId: 'sidecar-before' },
      restartKind: 'ungraceful_process',
      state: 'ready',
    })
    assert.deepEqual(events, [
      'probe-before',
      'arm-0',
      'arm-1',
      'control',
      'closed-0',
      'closed-1',
      'probe-after',
    ])
  })

  test('restart fails when the production probe does not prove a new advancing instance', async () => {
    const runWithTransition = async (before, after) => {
      let probeCalls = 0
      return performUngracefulRestart(
        restartControl(),
        'room-1',
        [],
        1_000,
        instanceProbe(),
        async (url) => {
          if (new URL(url).pathname === INSTANCE_PROBE_PATH) {
            const payload = probeCalls === 0 ? before : after
            probeCalls += 1
            return instanceResponse(instancePayload(payload))
          }
          return new Response(JSON.stringify(restartPayload()), {
            headers: { 'Content-Type': 'application/json' },
            status: 200,
          })
        },
      )
    }

    await assert.rejects(
      () => runWithTransition(
        { epoch: 11, instanceId: 'same-sidecar' },
        { epoch: 12, instanceId: 'same-sidecar' },
      ),
      /did not change instance_id/,
    )
    await assert.rejects(
      () => runWithTransition(
        { epoch: 11, instanceId: 'sidecar-before' },
        { epoch: 11, instanceId: 'sidecar-after' },
      ),
      /did not advance epoch/,
    )
  })

  test('real lease, session, and token fixture names are ignored while the example remains visible', () => {
    for (const path of [
      'tests/load/private.lease.json',
      'tests/load/private.session.json',
      'tests/load/private-lease-fixture.json',
      'tests/load/private-token-fixture.json',
    ]) {
      assert.equal(gitCheckIgnore(path), 0, `${path} must be ignored`)
    }
    assert.equal(gitCheckIgnore('tests/load/session-fixture.example.json'), 1)
  })
})

describe('raw Hocuspocus protocol', () => {
  test('one sendFrame call performs exactly one socket send', () => {
    const client = rawClient()
    const sent = []
    client.socket = {
      readyState: 1,
      send: (frame) => sent.push(frame),
    }

    client.sendFrame(MESSAGE_SYNC, encodeVarUint(SYNC_STEP_TWO), encodeBytes(new Uint8Array()))

    assert.equal(sent.length, 1)
    const decoder = new ByteDecoder(sent[0])
    assert.equal(decoder.readString(), client.session.room)
    assert.equal(decoder.readVarUint(), MESSAGE_SYNC)
    assert.equal(decoder.readVarUint(), SYNC_STEP_TWO)
    assert.deepEqual(decoder.readBytes(), new Uint8Array())
    decoder.assertDone()
  })

  test('one lease rotation sends one token sync and waits for a routed server sync proof', async () => {
    const client = rawClient()
    client.authenticated = true
    client.authenticatedScope = 'read-write'
    client.syncStepTwoReceived = true
    const sent = []
    client.socket = {
      readyState: 1,
      send: (frame) => sent.push(frame),
    }
    const now = Date.now() / 1_000
    const replacement = {
      ...client.session,
      expiresAt: now + 60,
      leaseToken: 'rotated-test-only-lease',
      refreshAfter: now + 45,
    }

    const rotation = client.rotateSession(replacement, 1_000)
    assert.equal(sent.length, 2)
    const authDecoder = new ByteDecoder(sent[0])
    assert.equal(authDecoder.readString(), client.session.room)
    assert.equal(authDecoder.readVarUint(), MESSAGE_AUTH)
    assert.equal(authDecoder.readVarUint(), AUTH_TOKEN)
    assert.equal(authDecoder.readString(), 'rotated-test-only-lease')
    assert.equal(typeof authDecoder.readString(), 'string')
    authDecoder.assertDone()
    const syncDecoder = new ByteDecoder(sent[1])
    assert.equal(syncDecoder.readString(), client.session.room)
    assert.equal(syncDecoder.readVarUint(), MESSAGE_SYNC)
    assert.equal(syncDecoder.readVarUint(), SYNC_STEP_ONE)
    assert(syncDecoder.readBytes().length > 0)
    syncDecoder.assertDone()
    assert.equal(client.authenticated, true)
    assert.equal(client.authenticatedScope, 'read-write')

    let settled = false
    void rotation.then(() => { settled = true })
    await Promise.resolve()
    assert.equal(settled, false)
    client.handleMessage(encodeRoutedFrame(
      client.session.room,
      MESSAGE_AUTH,
      encodeVarUint(AUTHENTICATED),
      encodeString('read-write'),
    ))
    await Promise.resolve()
    assert.equal(settled, false)
    const serverDocument = new Y.Doc()
    client.handleMessage(encodeRoutedFrame(
      client.session.room,
      MESSAGE_SYNC_REPLY,
      encodeVarUint(SYNC_STEP_TWO),
      encodeBytes(Y.encodeStateAsUpdate(serverDocument)),
    ))
    serverDocument.destroy()
    await rotation
    assert.equal(client.session, replacement)
    client.document.destroy()
  })

  test('lease rotation rejects denial, transport failure, and a missing sync proof', async () => {
    const replacement = (client) => ({
      ...client.session,
      expiresAt: Date.now() / 1_000 + 60,
      leaseToken: 'rotated-test-only-lease',
      refreshAfter: Date.now() / 1_000 + 45,
    })

    const denied = rawClient()
    denied.socket = { readyState: 1, send() {} }
    const deniedRotation = denied.rotateSession(replacement(denied), 1_000)
    assert.throws(
      () => denied.handleMessage(encodeRoutedFrame(
        denied.session.room,
        MESSAGE_AUTH,
        encodeVarUint(AUTH_DENIED),
        encodeString('invalid_lease'),
      )),
      /denied by collaboration authentication/,
    )
    await assert.rejects(deniedRotation, /denied by collaboration authentication/)
    denied.document.destroy()

    const closed = rawClient()
    closed.socket = { readyState: 1, send() {} }
    const closedRotation = closed.rotateSession(replacement(closed), 1_000)
    assert.throws(
      () => closed.fail(new Error('socket closed during rotation')),
      /socket closed during rotation/,
    )
    await assert.rejects(closedRotation, /socket closed during rotation/)
    closed.document.destroy()

    const timedOut = rawClient()
    timedOut.socket = { readyState: 1, send() {} }
    await assert.rejects(
      timedOut.rotateSession(replacement(timedOut), 5),
      /did not confirm its rotated lease through server sync/,
    )
    timedOut.document.destroy()
  })

  test('fresh observer authentication records an already expired lease as fatal before networking', async () => {
    const fatal = new FatalSocketState()
    const client = new RawCollaborationClient({
      index: 20,
      onFatal: (error) => fatal.record(error),
      session: session({ expiresAt: 1 }),
    })

    await assert.rejects(
      () => client.connect(1_000),
      /lease expired immediately before connection 20 authentication/,
    )
    assert.equal(client.socket, null)
    assert.throws(
      () => fatal.throwIfSet(),
      /lease expired immediately before connection 20 authentication/,
    )
    client.document.destroy()
  })

  test('controlled restart accepts only abnormal 1006 transport loss', async () => {
    const client = rawClient()
    client.socket = { readyState: 1 }

    client.expectUngracefulRestart()
    const plannedClose = client.waitForUngracefulRestartClose(100)
    client.restartExpectation.closeCode = 1012
    client.restartExpectation.resolve()
    await assert.rejects(
      plannedClose,
      /received close code 1012.*requires abnormal transport loss code 1006/,
    )

    client.expectUngracefulRestart()
    const abnormalClose = client.waitForUngracefulRestartClose(100)
    client.restartExpectation.closeCode = 1006
    client.restartExpectation.resolve()
    assert.equal(await abnormalClose, 1006)
    client.document.destroy()
  })

  test('routing, authenticated scope, and sync step two are all required for readiness', () => {
    const client = rawClient()
    let ready = false
    client.finishConnect = (error) => {
      assert.equal(error, null)
      ready = true
    }
    client.handleMessage(encodeRoutedFrame(
      client.session.room,
      MESSAGE_AUTH,
      encodeVarUint(AUTHENTICATED),
      encodeString('read-write'),
    ))
    assert.equal(client.authenticatedScope, 'read-write')
    assert.equal(ready, false)

    const emptyDocument = new Y.Doc()
    client.handleMessage(encodeRoutedFrame(
      client.session.room,
      MESSAGE_SYNC,
      encodeVarUint(SYNC_STEP_TWO),
      encodeBytes(Y.encodeStateAsUpdate(emptyDocument)),
    ))
    emptyDocument.destroy()

    assert.equal(client.syncStepTwoReceived, true)
    assert.equal(ready, true)
    client.document.destroy()
  })

  test('rejects a mismatched room, mismatched access scope, and trailing frame bytes', () => {
    const client = rawClient()
    const authenticated = encodeRoutedFrame(
      client.session.room,
      MESSAGE_AUTH,
      encodeVarUint(AUTHENTICATED),
      encodeString('read-write'),
    )

    assert.throws(
      () => client.handleMessage(encodeRoutedFrame(
        'another-room',
        MESSAGE_AUTH,
        encodeVarUint(AUTHENTICATED),
        encodeString('read-write'),
      )),
      /routing key/,
    )
    assert.throws(
      () => client.handleMessage(encodeRoutedFrame(
        client.session.room,
        MESSAGE_AUTH,
        encodeVarUint(AUTHENTICATED),
        encodeString('readonly'),
      )),
      /scope mismatch/,
    )
    assert.throws(
      () => client.handleMessage(concatBytes(authenticated, Uint8Array.of(0))),
      /trailing bytes/,
    )
    client.document.destroy()
  })

  test('preserves durable acknowledgement type, hash, and sequence', () => {
    const client = rawClient()
    const hash = 'a'.repeat(64)
    let acknowledgement = null
    client.onDurableAck = (value) => { acknowledgement = value }

    client.handleMessage(encodeRoutedFrame(
      client.session.room,
      MESSAGE_STATELESS,
      encodeString(JSON.stringify({ hash, sequence: 41, type: 'durable_ack' })),
    ))

    assert.deepEqual(acknowledgement, { hash, sequence: 41, type: 'durable_ack' })
    client.document.destroy()
  })

  test('accepts and counts exact document-bound comment events', () => {
    const client = rawClient()
    const events = []
    client.onCommentEvent = (event) => events.push(event)

    for (const type of [
      'collaboration_comment_changed',
      'collaboration_comment_mentioned',
    ]) {
      client.handleMessage(encodeRoutedFrame(
        client.session.room,
        MESSAGE_STATELESS,
        encodeString(JSON.stringify({ document_id: 'document', type })),
      ))
    }

    assert.equal(client.commentEventCount, 2)
    assert.deepEqual(events, [
      { documentId: 'document', type: 'collaboration_comment_changed' },
      { documentId: 'document', type: 'collaboration_comment_mentioned' },
    ])
    client.document.destroy()
  })

  test('keeps foreign, extended, unknown, malformed, and rejected stateless payloads fatal', () => {
    const client = rawClient()
    const invalid = [
      [
        JSON.stringify({
          document_id: 'another-document',
          type: 'collaboration_comment_changed',
        }),
        /targets another document/,
      ],
      [
        JSON.stringify({
          document_id: 'document',
          extra: true,
          type: 'collaboration_comment_changed',
        }),
        /invalid payload shape/,
      ],
      [JSON.stringify({ type: 'unknown_event' }), /type is unsupported/],
      ['{not-json', /not valid JSON/],
      [
        JSON.stringify({
          code: 'rejected',
          hash: 'a'.repeat(64),
          type: 'durable_rejection',
        }),
        /durability rejection is fatal/,
      ],
    ]

    for (const [payload, expected] of invalid) {
      assert.throws(
        () => client.handleMessage(encodeRoutedFrame(
          client.session.room,
          MESSAGE_STATELESS,
          encodeString(payload),
        )),
        expected,
      )
    }
    assert.equal(client.commentEventCount, 0)
    client.document.destroy()
  })

  test('preserves the safe stateless parser reason at the socket boundary', () => {
    let fatal = null
    const client = new RawCollaborationClient({
      index: 7,
      onFatal: (error) => { fatal = error },
      session: session(),
    })

    client.handleSocketMessage(encodeRoutedFrame(
      client.session.room,
      MESSAGE_STATELESS,
      encodeString('{not-json'),
    ), true)

    assert(fatal instanceof Error)
    assert.match(
      fatal.message,
      /Connection 7 received an invalid collaboration frame: Collaboration stateless payload is not valid JSON/,
    )
    assert.doesNotMatch(fatal.message, /\{not-json/)
    client.document.destroy()
  })

  test('reports valid collaboration close frames with their protocol reason', () => {
    for (const reason of ['service_unavailable', 'restarting']) {
      let fatal = null
      const client = new RawCollaborationClient({
        index: 7,
        onFatal: (error) => { fatal = error },
        session: session(),
      })

      client.handleSocketMessage(encodeRoutedFrame(
        client.session.room,
        MESSAGE_CLOSE,
        encodeString(reason),
      ), true)

      assert(fatal instanceof Error)
      assert.equal(
        fatal.message,
        `Connection 7 received a collaboration close frame: ${reason}.`,
      )
      assert.doesNotMatch(fatal.message, /invalid collaboration frame/)
      client.document.destroy()
    }
  })

  test('reports an explicit fallback for a valid close frame without a reason', () => {
    let fatal = null
    const client = new RawCollaborationClient({
      index: 3,
      onFatal: (error) => { fatal = error },
      session: session(),
    })

    client.handleSocketMessage(encodeRoutedFrame(
      client.session.room,
      MESSAGE_CLOSE,
    ), true)

    assert(fatal instanceof Error)
    assert.equal(
      fatal.message,
      'Connection 3 received a collaboration close frame: no reason provided.',
    )
    client.document.destroy()
  })

  test('keeps malformed or extended collaboration close frames classified as invalid', () => {
    const payloads = [
      Uint8Array.of(0x80),
      concatBytes(encodeString('restarting'), Uint8Array.of(0)),
    ]

    for (const payload of payloads) {
      let fatal = null
      const client = new RawCollaborationClient({
        index: 5,
        onFatal: (error) => { fatal = error },
        session: session(),
      })
      client.handleSocketMessage(encodeRoutedFrame(
        client.session.room,
        MESSAGE_CLOSE,
        payload,
      ), true)

      assert(fatal instanceof Error)
      assert.match(
        fatal.message,
        /Connection 5 received an invalid collaboration frame:/,
      )
      client.document.destroy()
    }
  })

  test('rejects durable acknowledgement sequence zero', () => {
    const client = rawClient()
    assert.throws(
      () => client.handleMessage(encodeRoutedFrame(
        client.session.room,
        MESSAGE_STATELESS,
        encodeString(JSON.stringify({
          hash: 'a'.repeat(64),
          sequence: 0,
          type: 'durable_ack',
        })),
      )),
      /not a durable acknowledgement/,
    )
    client.document.destroy()
  })
})

describe('latency and reconstruction gates', () => {
  test('writes explicit per-scenario outcomes for the selected load profile', async () => {
    const directory = mkdtempSync(join(tmpdir(), 'inqtrix-load-sidecar-'))
    const path = join(directory, 'scenarios.json')
    try {
      await writeScenarioResults(
        'capacity',
        {
          apiLatencyStatus: 'passed',
          apiSampleSpanPassed: true,
          durableAckPassed: true,
          minimumAckRoundsPassed: true,
          minimumDurationPassed: true,
          observerCohortPassed: true,
          visibleUpdatePassed: false,
        },
        { passed: true },
        { passed: true },
        { INQTRIX_VERIFICATION_SCENARIO_RESULTS_PATH: path },
      )
      const sidecar = JSON.parse(readFileSync(path, 'utf8'))
      assert.deepEqual(sidecar, {
        scenarios: [
          { id: 'load-capacity.latency', status: 'failed' },
          { id: 'load-capacity.rotation', status: 'passed' },
          { id: 'load-capacity.restart', status: 'passed' },
        ],
        schemaVersion: 1,
      })
    } finally {
      rmSync(directory, { force: true, recursive: true })
    }
  })

  test('soak results fail closed for supplemental activity evidence', async () => {
    const directory = mkdtempSync(join(tmpdir(), 'inqtrix-soak-sidecar-'))
    const path = join(directory, 'scenarios.json')
    try {
      const gates = {
        apiLatencyStatus: 'passed',
        apiSampleSpanPassed: true,
        durableAckPassed: true,
        minimumAckRoundsPassed: true,
        minimumDurationPassed: true,
        observerCohortPassed: true,
        phaseResultsPassed: true,
        visibleUpdatePassed: true,
      }
      await writeScenarioResults(
        'soak',
        gates,
        { passed: true },
        { passed: true },
        { INQTRIX_VERIFICATION_SCENARIO_RESULTS_PATH: path },
        {
          commentsAndNavigationPassed: true,
          featureActivityPassed: false,
          identityMatrixPassed: true,
          resourceRecoveryPassed: true,
        },
      )
      const sidecar = JSON.parse(readFileSync(path, 'utf8'))
      assert.deepEqual(sidecar.scenarios, [
        { id: 'load-soak.identity-matrix', status: 'passed' },
        { id: 'load-soak.comments-and-navigation', status: 'passed' },
        { id: 'load-soak.network-phases', status: 'passed' },
        { id: 'load-soak.durability', status: 'passed' },
        { id: 'load-soak.feature-activity', status: 'failed' },
        { id: 'load-soak.resource-recovery', status: 'passed' },
      ])
    } finally {
      rmSync(directory, { force: true, recursive: true })
    }
  })

  test('rotation supervisor covers every connected client and makes refresh failures fatal', async () => {
    const now = Date.now()
    const rotated = []
    const clients = [0, 1].map((index) => ({
      index,
      session: session({
        expiresAt: now / 1_000 + 30,
        leaseToken: `lease-${index}`,
        refreshAfter: now / 1_000 + 20,
        reissueId: `fixture-session-${index}`,
        userId: `user-${index}`,
      }),
      async rotateSession(replacement) {
        rotated.push(replacement.reissueId)
        this.session = replacement
      },
    }))
    const fatal = new FatalSocketState()
    const supervisor = new SessionRotationSupervisor({
      clients,
      concurrency: 1,
      control: sessionReissueControl(),
      fatal,
      reissue: async (_control, current, purpose) => {
        assert.equal(purpose, 'connected_rotation')
        return current.map((value, index) => ({
          ...value,
          expiresAt: now / 1_000 + 60,
          leaseToken: `replacement-${index}`,
          refreshAfter: now / 1_000 + 45,
        }))
      },
      timeoutMs: 1_000,
    })

    assert.equal(await supervisor.rotateNow('connected_rotation'), 2)
    assert.deepEqual(rotated, ['fixture-session-0', 'fixture-session-1'])
    assert.equal(supervisor.rotations.connected, 2)
    fatal.throwIfSet()

    const failed = new FatalSocketState()
    const failingSupervisor = new SessionRotationSupervisor({
      clients: [{
        session: session({
          expiresAt: now / 1_000 + 30,
          refreshAfter: now / 1_000 - 1,
        }),
      }],
      concurrency: 1,
      control: sessionReissueControl(),
      fatal: failed,
      now: () => now,
      reissue: async () => { throw new Error('authenticated session reissue failed') },
      timeoutMs: 1_000,
      wait: async () => {},
    })
    failingSupervisor.start()
    await new Promise((resolve) => setImmediate(resolve))
    assert.throws(() => failed.throwIfSet(), /authenticated session reissue failed/)
    await assert.rejects(
      () => failingSupervisor.stop(),
      /authenticated session reissue failed/,
    )
  })

  test('rotation synchronizes a ready client without waiting for a slow neighbor', async () => {
    const now = Date.now()
    const reissueCalls = []
    let releaseSlowReissue
    const slowReissue = new Promise((resolve) => { releaseSlowReissue = resolve })
    let markReadyClientRotated
    const readyClientRotated = new Promise((resolve) => {
      markReadyClientRotated = resolve
    })
    const clients = [0, 1].map((index) => ({
      index,
      session: session({
        expiresAt: now / 1_000 + 30,
        leaseToken: `lease-${index}`,
        refreshAfter: now / 1_000 + 20,
        reissueId: `independent-${index}`,
        userId: `independent-user-${index}`,
      }),
      async rotateSession(replacement) {
        this.session = replacement
        if (index === 1) markReadyClientRotated()
      },
    }))
    const fatal = new FatalSocketState()
    const supervisor = new SessionRotationSupervisor({
      clients,
      concurrency: 2,
      control: sessionReissueControl(),
      fatal,
      reissue: async (_control, current) => {
        reissueCalls.push(current.map((value) => value.reissueId))
        if (current.some((value) => value.reissueId === 'independent-0')) {
          await slowReissue
        }
        return current.map((value) => ({
          ...value,
          expiresAt: now / 1_000 + 60,
          leaseToken: `replacement-${value.reissueId}`,
          refreshAfter: now / 1_000 + 45,
        }))
      },
      timeoutMs: 1_000,
    })

    const rotation = supervisor.rotateNow('scheduled_rotation')
    const firstOutcome = await Promise.race([
      readyClientRotated.then(() => 'ready-client-rotated'),
      new Promise((resolve) => setTimeout(() => resolve('timeout'), 250)),
    ])
    releaseSlowReissue()
    assert.equal(await rotation, 2)

    assert.equal(firstOutcome, 'ready-client-rotated')
    assert.deepEqual(reissueCalls, [
      ['independent-0'],
      ['independent-1'],
    ])
    assert.equal(supervisor.rotations.scheduled, 2)
    fatal.throwIfSet()
  })

  test('rotation requires exactly one replacement for each client pipeline', async () => {
    const now = Date.now()
    for (const replacementCount of [0, 2]) {
      const current = session({
        expiresAt: now / 1_000 + 30,
        leaseToken: `lease-${replacementCount}`,
        refreshAfter: now / 1_000 + 20,
        reissueId: `replacement-count-${replacementCount}`,
        userId: `replacement-count-user-${replacementCount}`,
      })
      const fatal = new FatalSocketState()
      const supervisor = new SessionRotationSupervisor({
        clients: [{
          session: current,
          async rotateSession() {
            throw new Error('an ambiguous replacement must not reach socket sync')
          },
        }],
        concurrency: 1,
        control: sessionReissueControl(),
        fatal,
        reissue: async () => Array.from({ length: replacementCount }, (_, index) => ({
          ...current,
          expiresAt: now / 1_000 + 60,
          leaseToken: `replacement-${replacementCount}-${index}`,
          refreshAfter: now / 1_000 + 45,
        })),
        timeoutMs: 1_000,
      })

      await assert.rejects(
        () => supervisor.rotateNow('scheduled_rotation'),
        /exactly one replacement per client/,
      )
      assert.throws(
        () => fatal.throwIfSet(),
        /exactly one replacement per client/,
      )
      assert.equal(supervisor.rotations.scheduled, 0)
    }
  })

  test('explicit rotation rechecks each sequential batch after reissue before counting success', async () => {
    let now = 0
    const rotated = []
    const clients = [0, 1].map((index) => ({
      session: session({
        expiresAt: 1,
        leaseToken: `old-lease-${index}`,
        refreshAfter: 0.5,
        reissueId: `sequential-${index}`,
        userId: `sequential-user-${index}`,
      }),
      async rotateSession(replacement) {
        rotated.push(replacement.reissueId)
        this.session = replacement
      },
    }))
    const fatal = new FatalSocketState()
    const supervisor = new SessionRotationSupervisor({
      clients,
      concurrency: 1,
      control: sessionReissueControl(),
      fatal,
      now: () => now,
      reissue: async (_control, current) => {
        now += 750
        return current.map((value) => ({
          ...value,
          expiresAt: 60,
          leaseToken: `replacement-at-${now}`,
          refreshAfter: 45,
        }))
      },
      timeoutMs: 1_000,
    })

    await assert.rejects(
      () => supervisor.rotateNow('connected_rotation'),
      /lease expired after the session reissue response/,
    )
    assert.equal(now, 1_500)
    assert.deepEqual(rotated, ['sequential-0'])
    assert.equal(supervisor.rotations.connected, 0)
    assert.throws(
      () => fatal.throwIfSet(),
      /lease expired after the session reissue response/,
    )
  })

  test('scheduled rotation enforces the exact expiry boundary after response and before reauth', async () => {
    const runBoundary = async (times) => {
      let clockIndex = 0
      let rotations = 0
      const client = {
        session: session({ expiresAt: 1, refreshAfter: 0.5 }),
        async rotateSession(replacement) {
          rotations += 1
          this.session = replacement
        },
      }
      const fatal = new FatalSocketState()
      const supervisor = new SessionRotationSupervisor({
        clients: [client],
        concurrency: 1,
        control: sessionReissueControl(),
        fatal,
        now: () => times[Math.min(clockIndex++, times.length - 1)],
        reissue: async (_control, current) => current.map((value) => ({
          ...value,
          expiresAt: 60,
          leaseToken: 'boundary-replacement',
          refreshAfter: 45,
        })),
        timeoutMs: 1_000,
      })
      return { fatal, rotations: () => rotations, supervisor }
    }

    const beforeBoundary = await runBoundary([999, 999])
    assert.equal(await beforeBoundary.supervisor.rotateNow('scheduled_rotation'), 1)
    assert.equal(beforeBoundary.rotations(), 1)
    assert.equal(beforeBoundary.supervisor.rotations.scheduled, 1)
    beforeBoundary.fatal.throwIfSet()

    const atResponseBoundary = await runBoundary([1_000])
    await assert.rejects(
      () => atResponseBoundary.supervisor.rotateNow('scheduled_rotation'),
      /lease expired after the session reissue response/,
    )
    assert.equal(atResponseBoundary.rotations(), 0)
    assert.equal(atResponseBoundary.supervisor.rotations.scheduled, 0)
    assert.throws(() => atResponseBoundary.fatal.throwIfSet(), /lease expired/)

    const atReauthBoundary = await runBoundary([999, 1_000])
    await assert.rejects(
      () => atReauthBoundary.supervisor.rotateNow('scheduled_rotation'),
      /lease expired immediately before socket reauthentication/,
    )
    assert.equal(atReauthBoundary.rotations(), 0)
    assert.equal(atReauthBoundary.supervisor.rotations.scheduled, 0)
    assert.throws(() => atReauthBoundary.fatal.throwIfSet(), /lease expired/)
  })

  test('writers keep producing acknowledged samples for the loaded API probe window', async () => {
    const originalFetch = globalThis.fetch
    let probeCalls = 0
    globalThis.fetch = async () => {
      probeCalls += 1
      await new Promise((resolve) => setTimeout(resolve, 2))
      return new Response(JSON.stringify(healthPayload()), {
        headers: { 'Content-Type': 'application/json' },
        status: 200,
      })
    }
    const fatal = new FatalSocketState()
    const observers = [
      { index: 10, onVisibleUpdate: () => {} },
      { index: 11, onVisibleUpdate: () => {} },
    ]
    const writer = {
      createParagraphUpdate: (marker) => new TextEncoder().encode(marker),
      onDurableAck: () => {},
      sendUpdate(update) {
        const hash = createHash('sha256').update(update).digest('hex')
        setTimeout(() => {
          this.onDurableAck({ hash, sequence: 1, type: 'durable_ack' })
          for (const observer of observers) observer.onVisibleUpdate(hash)
        }, 1)
      },
    }
    try {
      const result = await runSustainedWriterLoad({
        apiProbe: apiProbe(),
        fatal,
        minAckRoundsPerWriter: 3,
        minDurationMs: 20,
        observers,
        sampleTimeoutMs: 1_000,
        writers: [writer],
      })

      assert.equal(probeCalls, 20)
      assert.ok(result.durationMs >= 20)
      assert.ok(result.roundsPerWriter[0] >= 3)
      assert.equal(result.observerCount, 2)
      assert.equal(result.visibleLatencies.length, result.markers.length * observers.length)
      assert.equal(result.durableLatencies.length, result.markers.length)
      assert.ok(result.loadedApiSampleSpanMs >= 20)
    } finally {
      globalThis.fetch = originalFetch
    }
  })

  test('soak phases remain separately budgeted and aggregate one paced marker family', async () => {
    const applied = []
    const runId = '01234567-89ab-4def-8123-456789abcdef'
    const phases = Array.from({ length: 6 }, (_, index) => ({
      durableAckP95Ms: 500,
      durationMs: 1,
      id: `phase-${index + 1}`,
      visibleUpdateP95Ms: 250,
    }))
    const result = await runSoakPhases({
      apiProbe: apiProbe(),
      applyPhase: async (_control, phaseId) => applied.push(phaseId),
      baselineApiMeasurement: { latencies: [10] },
      fatal: new FatalSocketState(),
      networkControl: {},
      observers: Array.from({ length: 10 }, (_, index) => ({ index })),
      phases,
      runId,
      runWriterLoad: async ({ markerTag, minDurationMs, writers }) => ({
        durableLatencies: [2],
        durationMs: minDurationMs,
        loadedApiLatencies: [10],
        loadedApiSampleSpanMs: minDurationMs,
        markers: writers.map((_, index) => `inqtrix-load-${runId}-${markerTag}-${index}-0`),
        observerCount: 10,
        roundsPerWriter: writers.map(() => 10),
        visibleLatencies: [1],
      }),
      sampleTimeoutMs: 1_000,
      writerIntervalMs: 5_000,
      writers: Array.from({ length: 5 }, () => ({})),
    })
    assert.deepEqual(applied, phases.map(({ id }) => id))
    assert.equal(result.phases.length, 6)
    assert.equal(result.markers.length, 30)
    assert.equal(result.gates.phaseResultsPassed, true)
    assert.equal(result.gates.minimumAckRoundsPassed, true)
    assert.equal(result.gates.minimumDurationPassed, true)
    assert.deepEqual(result.roundsPerWriter, [60, 60, 60, 60, 60])
  })

  test('soak API status reflects API samples rather than unrelated phase gates', async () => {
    const runId = '01234567-89ab-4def-8123-456789abcdef'
    const phases = Array.from({ length: 6 }, (_, index) => ({
      durableAckP95Ms: 500,
      durationMs: 1,
      id: `phase-${index + 1}`,
      visibleUpdateP95Ms: 1,
    }))
    const result = await runSoakPhases({
      apiProbe: apiProbe(),
      applyPhase: async () => undefined,
      baselineApiMeasurement: { latencies: [10] },
      fatal: new FatalSocketState(),
      networkControl: {},
      observers: Array.from({ length: 10 }, (_, index) => ({ index })),
      phases,
      runId,
      runWriterLoad: async ({ markerTag, minDurationMs, writers }) => ({
        durableLatencies: [2],
        durationMs: minDurationMs,
        loadedApiLatencies: [10],
        loadedApiSampleSpanMs: minDurationMs,
        markers: writers.map((_, index) => `inqtrix-load-${runId}-${markerTag}-${index}-0`),
        observerCount: 10,
        roundsPerWriter: writers.map(() => 10),
        visibleLatencies: [1],
      }),
      sampleTimeoutMs: 1_000,
      writers: Array.from({ length: 5 }, () => ({})),
    })
    assert.equal(result.gates.apiLatencyStatus, 'passed')
    assert.equal(result.gates.phaseResultsPassed, false)
    assert.equal(result.phases[0].gates.visibleUpdateStatus, 'failed')
    assert.equal(result.phases[0].gates.visibleUpdateHardLimitP95Ms, 1)
  })

  test('only smoke warns between the 250ms target and 300ms hard limit', () => {
    const options = parseArguments(['--mode', 'smoke'])
    const load = {
      durationMs: options.minDurationMs,
      loadedApiSampleSpanMs: options.minDurationMs,
      observerCount: options.observers,
      roundsPerWriter: Array.from(
        { length: options.writers },
        () => options.minAckRoundsPerWriter,
      ),
    }
    const gatesAt = (visibleP95Ms) => evaluateGates(
      [visibleP95Ms],
      [1],
      summarizeApiProbe([10], [10]),
      options,
      load,
    )
    const passed = gatesAt(249.999)
    const lowerWarning = gatesAt(250)
    const upperWarning = gatesAt(SMOKE_VISIBLE_WARNING_P95_MS)
    const failed = gatesAt(SMOKE_VISIBLE_WARNING_P95_MS + 0.001)

    assert.equal(passed.visibleUpdateStatus, 'passed')
    assert.equal(passed.visibleUpdatePassed, true)
    assert.equal(passed.visibleUpdateTargetP95Ms, 250)
    assert.equal(passed.visibleUpdateHardLimitP95Ms, SMOKE_VISIBLE_WARNING_P95_MS)
    assert.equal(lowerWarning.visibleUpdateStatus, 'warning')
    assert.equal(lowerWarning.visibleUpdatePassed, true)
    assert.equal(
      lowerWarning.visibleUpdateReason,
      'visible_update_above_target_within_smoke_limit',
    )
    assert.equal(upperWarning.visibleUpdateStatus, 'warning')
    assert.equal(upperWarning.visibleUpdatePassed, true)
    assert.match(
      formatVisibleUpdateGate({ p50: 100, p95: 270 }, upperWarning),
      /target<250ms hard<=300ms status=WARNING/,
    )
    assert.equal(failed.visibleUpdateStatus, 'failed')
    assert.equal(failed.visibleUpdatePassed, false)
    assert.equal(failed.visibleUpdateReason, 'visible_update_above_limit')
    assert.match(
      formatVisibleUpdateGate({ p50: 100, p95: 301 }, failed),
      /target<250ms hard<=300ms status=FAILED/,
    )
    assert.equal(allLoadGatesPassed(
      lowerWarning,
      { passed: true },
      { passed: true },
    ), true)
    assert.equal(allLoadGatesPassed(
      failed,
      { passed: true },
      { passed: true },
    ), false)
  })

  test('API p95 combines a hard absolute budget with a stable relative gate', () => {
    const apiProbe = summarizeApiProbe([10], [12])
    const degradedApiProbe = summarizeApiProbe([10], [12.01])
    const options = parseArguments(['--mode', 'capacity'])
    const gates = evaluateGates([250], [500], apiProbe, options, {
      durationMs: CAPACITY_MIN_DURATION_MS,
      loadedApiSampleSpanMs: CAPACITY_MIN_DURATION_MS,
      observerCount: CAPACITY_OBSERVER_COHORT,
      roundsPerWriter: Array.from(
        { length: CAPACITY_WRITERS },
        () => CAPACITY_MIN_ACK_ROUNDS_PER_WRITER,
      ),
    })

    assert.equal(gates.visibleUpdatePassed, false)
    assert.equal(gates.visibleUpdateStatus, 'failed')
    assert.equal(gates.visibleUpdateTargetP95Ms, CAPACITY_VISIBLE_P95_MS)
    assert.equal(gates.visibleUpdateHardLimitP95Ms, CAPACITY_VISIBLE_P95_MS)
    assert.equal(gates.durableAckPassed, false)
    assert.equal(gates.apiLatencyPassed, true)
    assert.equal(gates.apiLatencyAbsoluteLimitMs, API_P95_LIMIT_MS)
    assert.equal(gates.apiLatencyDegradationLimitPercent, API_DEGRADATION_LIMIT_PERCENT)
    assert.equal(
      gates.apiLatencyRelativeGateMinBaselineMs,
      API_RELATIVE_GATE_MIN_BASELINE_MS,
    )
    assert.equal(gates.apiLatencyRelativeGateApplied, true)
    assert.equal(degradedApiProbe.status, 'failed')
    assert.ok(degradedApiProbe.degradationPercent > 20)
    assert.equal(gates.minimumDurationPassed, true)
    assert.equal(gates.minimumAckRoundsPassed, true)
    assert.equal(gates.observerCohortPassed, true)
    assert.equal(gates.apiSampleSpanPassed, true)
    assert.equal(allLoadGatesPassed(
      gates,
      { passed: true },
      { passed: true },
    ), false)
  })

  test('single-digit API baselines warn on relative noise but never bypass 50ms', () => {
    const warning = summarizeApiProbe([3.9], [19.963])
    const boundary = summarizeApiProbe([3.9], [50])
    const absoluteFailure = summarizeApiProbe([3.9], [50.01])
    const quiet = summarizeApiProbe([9], [10])

    assert.equal(warning.status, 'warning')
    assert.equal(warning.absolutePassed, true)
    assert.equal(warning.relativeGateApplied, false)
    assert.equal(warning.relativePassed, false)
    assert.equal(warning.reason, 'relative_degradation_above_limit_below_stable_baseline')
    assert.match(formatApiProbe(warning), /absolute<=50ms PASS/)
    assert.match(formatApiProbe(warning), /relative<=20% WARN \(advisory; baseline below 10ms\)/)
    assert.match(formatApiProbe(warning), /status=WARNING/)
    assert.equal(boundary.status, 'warning')
    assert.equal(boundary.absolutePassed, true)
    assert.equal(absoluteFailure.status, 'failed')
    assert.equal(absoluteFailure.absolutePassed, false)
    assert.equal(absoluteFailure.reason, 'absolute_p95_above_limit')
    assert.match(formatApiProbe(absoluteFailure), /absolute<=50ms FAIL/)
    assert.match(formatApiProbe(absoluteFailure), /status=FAILED/)
    assert.equal(quiet.status, 'passed')
    assert.equal(quiet.relativeGateApplied, false)
    assert.equal(quiet.relativePassed, true)
  })

  test('soak preserves non-blocking API warnings across phase aggregation', async () => {
    const runId = '01234567-89ab-4def-8123-456789abcdef'
    const phases = Array.from({ length: 6 }, (_, index) => ({
      durableAckP95Ms: 500,
      durationMs: 1,
      id: `phase-${index + 1}`,
      visibleUpdateP95Ms: 250,
    }))
    const result = await runSoakPhases({
      apiProbe: apiProbe(),
      applyPhase: async () => undefined,
      baselineApiMeasurement: { latencies: [3.9] },
      fatal: new FatalSocketState(),
      networkControl: {},
      observers: Array.from({ length: 10 }, (_, index) => ({ index })),
      phases,
      runId,
      runWriterLoad: async ({ markerTag, minDurationMs, writers }) => ({
        durableLatencies: [2],
        durationMs: minDurationMs,
        loadedApiLatencies: [19.963],
        loadedApiSampleSpanMs: minDurationMs,
        markers: writers.map((_, index) => `inqtrix-load-${runId}-${markerTag}-${index}-0`),
        observerCount: 10,
        roundsPerWriter: writers.map(() => 10),
        visibleLatencies: [1],
      }),
      sampleTimeoutMs: 1_000,
      writers: Array.from({ length: 5 }, () => ({})),
    })

    assert.equal(result.gates.apiLatencyStatus, 'warning')
    assert.equal(result.gates.apiLatencyPassed, true)
    assert.equal(result.gates.phaseResultsPassed, true)
    assert.equal(result.phases.every((phase) => phase.apiProbe.status === 'warning'), true)
  })

  test('a missing exercised lease rotation fails an otherwise passing load result', () => {
    const options = parseArguments(['--mode', 'capacity'])
    const gates = evaluateGates([1], [1], summarizeApiProbe([10], [10]), options, {
      durationMs: CAPACITY_MIN_DURATION_MS,
      loadedApiSampleSpanMs: CAPACITY_MIN_DURATION_MS,
      observerCount: CAPACITY_OBSERVER_COHORT,
      roundsPerWriter: Array.from(
        { length: CAPACITY_WRITERS },
        () => CAPACITY_MIN_ACK_ROUNDS_PER_WRITER,
      ),
    })

    assert.equal(allLoadGatesPassed(gates, { passed: true }, { passed: true }), true)
    assert.equal(allLoadGatesPassed(gates, { passed: true }, { passed: false }), false)
  })

  test('duration, rounds, and observer cohort are independent capacity failures', () => {
    const options = parseArguments(['--mode', 'capacity'])
    const gates = evaluateGates([1], [1], summarizeApiProbe([10], [10]), options, {
      durationMs: CAPACITY_MIN_DURATION_MS - 1,
      loadedApiSampleSpanMs: CAPACITY_MIN_DURATION_MS - 1,
      observerCount: CAPACITY_OBSERVER_COHORT - 1,
      roundsPerWriter: Array.from(
        { length: CAPACITY_WRITERS },
        () => CAPACITY_MIN_ACK_ROUNDS_PER_WRITER - 1,
      ),
    })

    assert.equal(gates.minimumDurationPassed, false)
    assert.equal(gates.minimumAckRoundsPassed, false)
    assert.equal(gates.observerCohortPassed, false)
    assert.equal(gates.apiSampleSpanPassed, false)
  })

  test('explicit smoke API opt-out remains visibly skipped', () => {
    const apiProbe = summarizeApiProbe(null, null)

    assert.equal(apiProbe.status, 'skipped')
    assert.equal(apiProbe.reason, 'explicit_load_smoke_api_probe_opt_out')
  })

  test('fresh reconstruction fails on duplicates, missing, or unexpected markers', () => {
    const runId = 'run-id'
    const markers = [
      `inqtrix-load-${runId}-0-0`,
      `inqtrix-load-${runId}-1-0`,
    ]
    const client = {
      documentText: () => `${markers[0]} ${markers[0]} inqtrix-load-${runId}-9-9`,
    }

    assert.deepEqual(verifyReconstructedMarkers(client, markers, runId), {
      duplicates: 1,
      expected: 2,
      missing: 1,
      observed: 3,
      passed: false,
      unexpected: 1,
    })
  })

  test('fresh observer cohort requires exact final state on every observer', () => {
    const runId = 'cohort-run'
    const markers = [
      `inqtrix-load-${runId}-0-0`,
      `inqtrix-load-${runId}-1-0`,
    ]
    const result = verifyObserverCohort([
      { documentText: () => markers.join(' ') },
      { documentText: () => `${markers[0]} ${markers[0]}` },
    ], markers, runId)

    assert.equal(result.observerCount, 2)
    assert.equal(result.failedObservers, 1)
    assert.equal(result.passed, false)
    assert.equal(result.missing, 1)
    assert.equal(result.duplicates, 1)
  })

  test('a socket failure remains fatal after samples are complete', () => {
    const fatal = new FatalSocketState()
    fatal.record(new Error('socket closed after samples'))

    assert.throws(() => fatal.throwIfSet(), /after samples/)
  })
})

describe('accepted-latency warning band', () => {
  const budgets = {
    durableAckP95Ms: 10_000,
    minAckRoundsPerWriter: 1,
    minDurationMs: 0,
    observers: 1,
    visibleUpdateP95Ms: 250,
    writers: 1,
  }
  const load = {
    durationMs: 1_000,
    loadedApiSampleSpanMs: 1_000,
    observerCount: 1,
    roundsPerWriter: [5],
  }
  const probe = { relativeGateApplied: false, status: 'skipped' }

  function gatesFor(mode, visibleP95) {
    return evaluateGates(
      [visibleP95],
      [10],
      probe,
      { ...budgets, mode },
      load,
    )
  }

  test('soak and ramp warn above the accepted target instead of failing', () => {
    // The accepted large-state latency risk is an explicit architecture
    // decision: the visible p95 is known to sit above 250ms under
    // sustained load. Reporting a known, accepted risk as a hard failure
    // adds no information and would keep CI permanently red.
    for (const mode of ['soak', 'ramp']) {
      const gates = gatesFor(mode, 400)
      assert.equal(gates.visibleUpdateStatus, 'warning', mode)
      assert.equal(gates.visibleUpdatePassed, true, mode)
      // The target and the raw value stay visible — a warning must never
      // read as if the budget had been met.
      assert.equal(gates.visibleUpdateTargetP95Ms, 250, mode)
      assert.equal(gates.visibleUpdateReason, 'visible_update_above_accepted_target', mode)
    }
  })

  test('a catastrophic visible latency still fails soak and ramp', () => {
    // The band is not unbounded: an update that takes seconds to appear
    // is a functional defect, not a latency nuance.
    for (const mode of ['soak', 'ramp']) {
      const gates = gatesFor(mode, ACCEPTED_LATENCY_CEILING_P95_MS + 1)
      assert.equal(gates.visibleUpdateStatus, 'failed', mode)
      assert.equal(gates.visibleUpdatePassed, false, mode)
    }
  })

  test('capacity keeps its hard production budget', () => {
    // The production gate may never inherit the local accepted risk.
    const gates = gatesFor('capacity', 400)

    assert.equal(gates.visibleUpdateStatus, 'failed')
    assert.equal(gates.visibleUpdatePassed, false)
  })

  test('a visible p95 within target still passes cleanly', () => {
    for (const mode of ['soak', 'ramp', 'capacity']) {
      const gates = gatesFor(mode, 100)
      assert.equal(gates.visibleUpdateStatus, 'passed', mode)
      assert.equal(gates.visibleUpdateReason, null, mode)
    }
  })
})

describe('local capacity ramp', () => {
  test('plans monotonic stages within the identity ceiling', () => {
    const stages = planRampStages()

    assert.deepEqual(stages.map((stage) => stage.connections), [...RAMP_STAGES])
    for (const [index, stage] of stages.entries()) {
      assert(stage.writers >= 1 && stage.writers <= stage.connections)
      assert(stage.observers >= 1 && stage.observers < stage.connections)
      // Every socket must come from a real identity, and the local
      // ceiling is what makes this a fan-out proof rather than a
      // capacity release: the plan states it instead of implying it.
      assert(stage.sessionsPerIdentity * RAMP_MAX_IDENTITIES >= stage.connections)
      if (index > 0) {
        assert(stage.connections > stages[index - 1].connections)
        assert(stage.writers >= stages[index - 1].writers)
      }
    }
  })

  test('never plans a rung that would need the product session cap widened', () => {
    const { ceiling, reachable, unreachable } = partitionRampStages()

    // 24 identities x 5 sessions per user and document = 120 sockets.
    assert.equal(ceiling, RAMP_MAX_IDENTITIES * RAMP_SESSIONS_PER_IDENTITY)
    assert.equal(rampSocketCeiling(), ceiling)
    assert.deepEqual(reachable, [20, 100])
    assert.deepEqual(unreachable, [250, 500, 1_000])
    // Every planned rung must fit the cap WITHOUT reconfiguring the
    // product: asking for a weakened abuse guard to make a test pass is
    // exactly what this assertion exists to prevent.
    for (const stage of planRampStages(reachable)) {
      assert(stage.sessionsPerIdentity <= RAMP_SESSIONS_PER_IDENTITY)
    }
  })

  test('a stopped ladder is not an integrity failure', () => {
    // Guards the exit-code rule: a budget stop must never be reported
    // with the same severity as a lost or duplicated mutation.
    const budget = summarizeRamp([
      { connections: 20, integrityPassed: true, latencyPassed: true },
      { connections: 100, integrityPassed: true, latencyPassed: false },
    ], [20, 100])
    const integrity = summarizeRamp([
      { connections: 20, integrityPassed: false, latencyPassed: true },
    ], [20, 100])

    assert.equal(budget.stopKind, 'budget')
    assert.notEqual(budget.status, 'failed')
    assert.equal(integrity.stopKind, 'integrity')
    assert.equal(integrity.status, 'failed')
  })

  test('an integrity failure is red and stops the ramp', () => {
    const summary = summarizeRamp([
      { connections: 20, integrityPassed: true, latencyPassed: true },
      { connections: 100, integrityPassed: false, latencyPassed: true, reason: 'missing mutation' },
    ])

    assert.equal(summary.status, 'failed')
    assert.equal(summary.highestPassedConnections, 20)
    assert.equal(summary.stopKind, 'integrity')
    assert.match(summary.reason, /missing mutation/)
  })

  test('a headroom or latency stop reports the reached stage without turning red', () => {
    const summary = summarizeRamp([
      { connections: 20, integrityPassed: true, latencyPassed: true },
      { connections: 100, integrityPassed: true, latencyPassed: true },
      { connections: 250, integrityPassed: true, latencyPassed: false, reason: 'visible p95 above budget' },
    ])

    assert.equal(summary.status, 'stopped')
    assert.equal(summary.highestPassedConnections, 100)
    assert.equal(summary.stopKind, 'budget')
    // The unreached rungs stay visible as an OPEN production-capacity
    // proof — a controlled stop may never read as full coverage.
    assert.deepEqual(summary.unreachedConnections, [250, 500, 1_000])
    assert.equal(summary.productionProofOutstanding, true)
  })

  test('a complete ramp still leaves the production proof outstanding', () => {
    const summary = summarizeRamp(
      RAMP_STAGES.map((connections) => ({
        connections,
        integrityPassed: true,
        latencyPassed: true,
      })),
    )

    assert.equal(summary.status, 'passed')
    assert.equal(summary.highestPassedConnections, 1_000)
    assert.deepEqual(summary.unreachedConnections, [])
    // Even a full local ramp is fan-out from at most 24 identities.
    assert.equal(summary.productionProofOutstanding, true)
    assert.equal(summary.identityCeiling, RAMP_MAX_IDENTITIES)
  })
})

function rawClient() {
  return new RawCollaborationClient({
    index: 0,
    onFatal: (error) => { throw error },
    session: session(),
  })
}

function gitCheckIgnore(path) {
  return spawnSync('git', ['check-ignore', '-q', '--', path], {
    cwd: new URL('../..', import.meta.url),
  }).status
}

function apiProbe(url = 'https://collaboration.example.test/health') {
  return { contract: API_PROBE_CONTRACT, url: new URL(url) }
}

function instanceProbe(
  url = `https://collaboration.example.test${INSTANCE_PROBE_PATH}`,
) {
  return { contract: INSTANCE_PROBE_CONTRACT, url: new URL(url) }
}

function instancePayload({ epoch = 11, instanceId = 'sidecar-before' } = {}) {
  return {
    contract: INSTANCE_PROBE_CONTRACT,
    epoch,
    instance_id: instanceId,
    service: 'inqtrix-collaboration',
    status: 'ready',
  }
}

function instanceResponse(payload) {
  return new Response(JSON.stringify(payload), {
    headers: {
      'Cache-Control': 'no-store',
      'Content-Type': 'application/json',
    },
    status: 200,
  })
}

function healthPayload() {
  return {
    auth_mode: 'oidc',
    legal: { imprint_url: null, privacy_url: null },
    llm: { provider: 'test-llm', status: 'ready' },
    search: { provider: 'test-search', status: 'ready' },
    status: 'ok',
  }
}

function restartControl() {
  return {
    authorization: 'Bearer test-control-value',
    url: new URL('https://control.example.test/restart'),
  }
}

function sessionReissueControl(overrides = {}) {
  return {
    authorization: 'Bearer test-reissue-value',
    contract: SESSION_REISSUE_CONTRACT,
    leaseTtlSeconds: CAPACITY_LEASE_TTL_SECONDS,
    runId: 'inqv-load-reissue-test-01',
    url: new URL('https://control.example.test/reissue'),
    ...overrides,
  }
}

function reissueResponse(existing, rotationCommandId, overrides = {}) {
  const expiresAt = overrides.expiresAt ?? Date.now() / 1_000 + 60
  const refreshAfter = overrides.refreshAfter ?? Date.now() / 1_000 + 45
  const userId = overrides.userId ?? existing.userId
  return new Response(JSON.stringify({
    contract: SESSION_REISSUE_CONTRACT,
    lease_ttl_seconds: CAPACITY_LEASE_TTL_SECONDS,
    sessions: [{
      reissue_id: existing.reissueId,
      rotation_command_id: rotationCommandId,
      session: {
        access: existing.access,
        expires_at: expiresAt,
        initial_write_mode: existing.access,
        lease_token: overrides.leaseToken ?? 'reissued-test-only-lease',
        protocol_version: existing.protocolVersion,
        refresh_after: refreshAfter,
        room: existing.room,
        schema_version: existing.schemaVersion,
        user: { id: userId },
        websocket_path: new URL(existing.websocketUrl).pathname,
      },
    }],
    source: 'fastapi_collaboration_session',
  }), {
    headers: {
      'Cache-Control': 'no-store',
      'Content-Type': 'application/json',
    },
    status: 200,
  })
}

function restartPayload({
  afterEpoch = 12,
  afterId = 'sidecar-after',
  beforeEpoch = 11,
  beforeId = 'sidecar-before',
} = {}) {
  return {
    after: { epoch: afterEpoch, instance_id: afterId },
    before: { epoch: beforeEpoch, instance_id: beforeId },
    restart_kind: 'ungraceful_process',
    state: 'ready',
  }
}

function session(overrides = {}) {
  return {
    access: 'edit',
    expiresAt: 4_102_444_800,
    leaseToken: 'test-only-lease-value',
    origin: 'https://collaboration.example.test',
    protocolVersion: 1,
    refreshAfter: 4_102_444_700,
    reissueId: 'fixture-session-0',
    room: 'inqtrix-editor-v1:document:g1',
    schemaVersion: 1,
    userId: 'user-0',
    websocketUrl: 'wss://collaboration.example.test/collaboration',
    ...overrides,
  }
}
