import assert from 'node:assert/strict'
import {
  mkdirSync,
  mkdtempSync,
  rmSync,
  writeFileSync,
} from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { describe, test } from 'node:test'

import {
  assertStrictE2EConfiguration,
  guestLinkGateReason,
  loadCollaborationE2EConfiguration,
  strictPreflightReasons,
  resolveCollaborationE2EMode,
} from './config.ts'

describe('collaboration E2E profile preflight', () => {
  test('developer mode reports an absent fixture without pretending it ran', () => {
    const configuration = loadCollaborationE2EConfiguration({}, '/workspace')

    assert.equal(configuration.mode, 'dev')
    assert.equal(configuration.stack, null)
    assert.deepEqual(configuration.reasons, ['INQTRIX_E2E_FIXTURE is not set'])
  })

  test('strict mode rejects an absent fixture', () => {
    const environment = { INQTRIX_E2E_MODE: 'strict' }
    const configuration = loadCollaborationE2EConfiguration(environment, '/workspace')

    assert.throws(
      () => assertStrictE2EConfiguration(configuration, 'system-smoke', environment),
      /INQTRIX_E2E_FIXTURE is not set/,
    )
  })

  test('fault-injection requires private anchors, controls, and fault documents', () => {
    const complete = validFixture()
    const fixture = {
      ...complete,
      controls: undefined,
      documents: {
        directEdit: complete.documents.directEdit,
        revocation: complete.documents.revocation,
        suggestion: complete.documents.suggestion,
      },
      privateAnchors: undefined,
    }
    const { configuration, environment } = loadFixture(fixture)

    assert.deepEqual(strictPreflightReasons(configuration, 'fault-injection', environment), [
      'fixture.documents.downgrade is required for fault-injection',
      'fixture.documents.gatewayOutage is required for fault-injection',
      'fixture.documents.reconciliation is required for fault-injection',
      'fixture.documents.outage is required for fault-injection',
      'fixture.documents.protocol is required for fault-injection',
      'fixture.privateAnchors is required for fault-injection',
      'fixture.controls is required for fault-injection',
    ])
  })

  test('system-smoke requires the detached project-transfer document', () => {
    const fixture: any = validFixture()
    delete fixture.documents.detachedTransfer
    const { configuration, environment } = loadFixture(fixture)

    assert.deepEqual(strictPreflightReasons(configuration, 'system-smoke', environment), [
      'fixture.documents.detachedTransfer is required for system-smoke',
    ])
  })

  test('strict profiles reject path aliases on one origin', () => {
    const fixture: any = validFixture()
    fixture.transports['python-gateway'].baseURL =
      'https://vite.example.test/python-gateway/'
    const { configuration, environment } = loadFixture(fixture)

    assert.match(
      strictPreflightReasons(configuration, 'system-smoke', environment).join('\n'),
      /must use three distinct origins/,
    )
  })

  test('a generated run-private fixture scopes system-smoke to its active transport', () => {
    const directory = mkdtempSync(join(tmpdir(), 'inqtrix-e2e-generated-config-'))
    const reportDirectory = join(directory, 'report')
    const privateDirectory = join(reportDirectory, '.cleanup-secrets')
    mkdirSync(privateDirectory, { mode: 0o700, recursive: true })
    writeFileSync(join(privateDirectory, 'owner.json'), '{}', { mode: 0o600 })
    writeFileSync(join(privateDirectory, 'collaborator.json'), '{}', { mode: 0o600 })
    const fixture: any = validFixture()
    fixture.execution = {
      contract: 'inqtrix-generated-system-smoke-v1',
      runId: 'inqv-generated-system-01',
      transport: 'python-gateway',
    }
    fixture.transports = {
      'python-gateway': {
        baseURL: 'https://python-gateway.example.test',
      },
    }
    const fixturePath = join(privateDirectory, 'fixture.json')
    writeFileSync(fixturePath, JSON.stringify(fixture), { mode: 0o600 })
    const environment = {
      INQTRIX_E2E_FIXTURE: fixturePath,
      INQTRIX_E2E_MODE: 'strict',
      INQTRIX_VERIFICATION_PROFILE: 'system-smoke',
      INQTRIX_VERIFICATION_REPORT_DIR: reportDirectory,
      INQTRIX_VERIFICATION_RUN_ID: 'inqv-generated-system-01',
    }

    const configuration = loadCollaborationE2EConfiguration(
      environment,
      directory,
    )

    assert.deepEqual(configuration.selectedTransports, ['python-gateway'])
    assert.deepEqual(
      strictPreflightReasons(configuration, 'system-smoke', environment),
      [],
    )
  })

  test('a generated run-private fault fixture keeps its active transport and run-scoped control', () => {
    const directory = mkdtempSync(join(tmpdir(), 'inqtrix-e2e-generated-fault-config-'))
    const reportDirectory = join(directory, 'report')
    const privateDirectory = join(reportDirectory, '.cleanup-secrets')
    mkdirSync(privateDirectory, { mode: 0o700, recursive: true })
    writeFileSync(join(privateDirectory, 'owner.json'), '{}', { mode: 0o600 })
    writeFileSync(join(privateDirectory, 'collaborator.json'), '{}', { mode: 0o600 })
    const fixture: any = validFixture()
    fixture.execution = {
      contract: 'inqtrix-generated-fault-injection-v1',
      runId: 'inqv-generated-fault-01',
      transport: 'python-gateway',
    }
    fixture.controls = {
      ...fixture.controls,
      baseURL: 'http://127.0.0.1:43123',
      runId: 'inqv-generated-fault-01',
    }
    fixture.transports = {
      'python-gateway': {
        baseURL: 'https://127.0.0.1:8080',
      },
    }
    const fixturePath = join(privateDirectory, 'fixture.json')
    writeFileSync(fixturePath, JSON.stringify(fixture), { mode: 0o600 })
    const environment = {
      INQTRIX_E2E_CONTROL_TOKEN: 'test-only-control-token',
      INQTRIX_E2E_FIXTURE: fixturePath,
      INQTRIX_E2E_MODE: 'strict',
      INQTRIX_VERIFICATION_PROFILE: 'fault-injection',
      INQTRIX_VERIFICATION_REPORT_DIR: reportDirectory,
      INQTRIX_VERIFICATION_RUN_ID: 'inqv-generated-fault-01',
    }

    const configuration = loadCollaborationE2EConfiguration(
      environment,
      directory,
    )

    assert.deepEqual(configuration.selectedTransports, ['python-gateway'])
    assert.equal(configuration.stack?.controls?.runId, 'inqv-generated-fault-01')
    assert.deepEqual(
      strictPreflightReasons(configuration, 'fault-injection', environment),
      [],
    )
  })

  test('an external fixture cannot opt out of the three-transport contract', () => {
    const directory = mkdtempSync(join(tmpdir(), 'inqtrix-e2e-external-scope-'))
    try {
      const fixture: any = validFixture()
      fixture.execution = {
        contract: 'inqtrix-generated-system-smoke-v1',
        runId: 'inqv-external-system-01',
        transport: 'python-gateway',
      }
      fixture.transports = {
        'python-gateway': {
          baseURL: 'https://python-gateway.example.test',
        },
      }
      writeFileSync(join(directory, 'owner.json'), '{}', { mode: 0o600 })
      writeFileSync(join(directory, 'collaborator.json'), '{}', { mode: 0o600 })
      const fixturePath = join(directory, 'fixture.json')
      writeFileSync(fixturePath, JSON.stringify(fixture), { mode: 0o600 })
      const environment = {
        INQTRIX_E2E_FIXTURE: fixturePath,
        INQTRIX_E2E_MODE: 'strict',
        INQTRIX_VERIFICATION_PROFILE: 'system-smoke',
        INQTRIX_VERIFICATION_REPORT_DIR: join(directory, 'report'),
        INQTRIX_VERIFICATION_RUN_ID: 'inqv-external-system-01',
      }

      const configuration = loadCollaborationE2EConfiguration(
        environment,
        directory,
      )
      const reasons = strictPreflightReasons(
        configuration,
        'system-smoke',
        environment,
      ).join('\n')

      assert.deepEqual(
        configuration.selectedTransports,
        ['vite', 'nginx', 'python-gateway'],
      )
      assert.match(reasons, /private run directory/)
      assert.match(reasons, /INQTRIX_E2E_VITE_BASE_URL/)
      assert.match(reasons, /INQTRIX_E2E_NGINX_BASE_URL/)
    } finally {
      rmSync(directory, { force: true, recursive: true })
    }
  })

  test('strict profiles require two distinct declared identities and state files', () => {
    const sameIdentity = validFixture()
    sameIdentity.users.owner.userId = sameIdentity.users.collaborator.userId
    sameIdentity.users.owner.storageState =
      sameIdentity.users.collaborator.storageState
    const { configuration, environment } = loadFixture(sameIdentity)
    const reasons = strictPreflightReasons(
      configuration,
      'system-smoke',
      environment,
    ).join('\n')

    assert.match(reasons, /owner and collaborator user IDs must be distinct/)
    assert.match(reasons, /owner and collaborator storageState files must be distinct/)
  })

  test('preflight diagnostics never expose fixture or storage-state paths', () => {
    const missing = loadCollaborationE2EConfiguration(
      {
        INQTRIX_E2E_FIXTURE: '/private/fixture-owner@example.invalid.json',
        INQTRIX_E2E_MODE: 'strict',
      },
      '/workspace',
    )

    assert.deepEqual(missing.reasons, ['INQTRIX_E2E_FIXTURE does not exist'])
    assert.doesNotMatch(JSON.stringify(missing), /private|owner@example/)
  })

  test('strict profiles reject group-readable browser credential state', () => {
    if (process.platform === 'win32') return
    const fixture = validFixture()
    const { configuration, environment } = loadFixture(fixture, {
      ownerMode: 0o640,
    })

    assert.match(
      strictPreflightReasons(
        configuration,
        'system-smoke',
        environment,
      ).join('\n'),
      /owner storageState must not be accessible by group or other users/,
    )
  })

  test('configured application base paths remain navigable roots', () => {
    const fixture = validFixture()
    fixture.transports.vite.baseURL = 'https://vite.example.test/inqtrix'
    const { configuration } = loadFixture(fixture)

    assert.equal(
      configuration.stack?.transports.vite.baseURL,
      'https://vite.example.test/inqtrix/',
    )
  })

  test('strict profiles reject transport URLs that could carry credentials', () => {
    const fixture = validFixture()
    fixture.transports['python-gateway'].baseURL =
      'https://user:password@python-gateway.example.test'
    const { configuration, environment } = loadFixture(fixture)

    assert.match(
      strictPreflightReasons(configuration, 'system-smoke', environment).join('\n'),
      /python-gateway baseURL must not contain credentials/,
    )
  })

  test('fault-injection requires concrete private anchor ranges, not marker cards alone', () => {
    const fixture = validFixture()
    fixture.privateAnchors.owner.aiAnchorText = ''
    const { configuration, environment } = loadFixture(fixture)

    assert.match(
      strictPreflightReasons(configuration, 'fault-injection', environment).join('\n'),
      /fixture\.privateAnchors\.owner\.aiAnchorText must be a non-empty string/,
    )
    assert.match(
      strictPreflightReasons(configuration, 'fault-injection', environment).join('\n'),
      /fixture\.privateAnchors is required for fault-injection/,
    )
  })

  test('fault-injection requires an isolated private-anchor document per browser target', () => {
    const fixture: any = validFixture()
    delete fixture.privateAnchors.documents['webkit-desktop']
    const { configuration, environment } = loadFixture(fixture)

    assert.match(
      strictPreflightReasons(configuration, 'fault-injection', environment).join('\n'),
      /fixture\.privateAnchors\.documents\.webkit-desktop must be a non-empty string/,
    )
    assert.match(
      strictPreflightReasons(configuration, 'fault-injection', environment).join('\n'),
      /fixture\.privateAnchors is required for fault-injection/,
    )
  })

  test('strict profiles bind the suggestion document to a real suggest principal', () => {
    const permissionFixture = validFixture()
    permissionFixture.documents.suggestion.expectedPermission = 'edit'
    const permission = loadFixture(permissionFixture)
    assert.match(
      strictPreflightReasons(
        permission.configuration,
        'system-smoke',
        permission.environment,
      ).join('\n'),
      /expectedPermission must equal "suggest"/,
    )

    const identityFixture = validFixture()
    identityFixture.documents.suggestion.expectedAuthorId = '00000000-0000-4000-8000-000000000003'
    const identity = loadFixture(identityFixture)
    assert.match(
      strictPreflightReasons(
        identity.configuration,
        'system-smoke',
        identity.environment,
      ).join('\n'),
      /expectedAuthorId must equal fixture\.users\.collaborator\.userId/,
    )
  })

  test('fault-injection requires a distinct FastAPI gateway fault document and control', () => {
    const fixture = validFixture()
    fixture.documents.gatewayOutage = ''
    fixture.controls.armGatewayOutagePath = ''
    const { configuration, environment } = loadFixture(fixture)
    const reasons = strictPreflightReasons(
      configuration,
      'fault-injection',
      environment,
    ).join('\n')

    assert.match(reasons, /fixture\.documents\.gatewayOutage is required for fault-injection/)
    assert.match(reasons, /fixture\.controls\.armGatewayOutagePath must be a non-empty string/)
    assert.match(reasons, /fixture\.controls is required for fault-injection/)
  })

  test('a complete fault-injection fixture passes without weakening prerequisites', () => {
    const { configuration, environment } = loadFixture(validFixture())

    assert.equal(configuration.mode, 'strict')
    assert.deepEqual(
      strictPreflightReasons(configuration, 'fault-injection', environment),
      [],
    )
    assert.doesNotThrow(() => assertStrictE2EConfiguration(
      configuration,
      'fault-injection',
      environment,
    ))
  })

  test('fault-injection fails when the named control authorization value is absent', () => {
    const { configuration, environment } = loadFixture(validFixture())
    delete environment.INQTRIX_E2E_CONTROL_TOKEN

    assert.deepEqual(strictPreflightReasons(
      configuration,
      'fault-injection',
      environment,
    ), [
      'INQTRIX_E2E_CONTROL_TOKEN is required for fault-control authorization',
    ])
  })

  test('mode parsing rejects ambiguous values', () => {
    assert.equal(resolveCollaborationE2EMode({}), 'dev')
    assert.throws(
      () => resolveCollaborationE2EMode({ INQTRIX_E2E_MODE: 'gate' }),
      /must be "dev" or "strict"/,
    )
  })
})

describe('guest-link gate precondition', () => {
  test('an HTTPS origin carries no reason to skip', () => {
    assert.equal(guestLinkGateReason('https://inqtrix.example.test', true), null)
  })

  test('an HTTP origin names the unmet precondition instead of failing later', () => {
    // Guest links depend on a trusted secure context (Secure cookies,
    // HTTPS origin, WSS). On an HTTP stack the section cannot run at
    // all — that has to be decided BEFORE the six-user matrix invests
    // minutes of work, and it has to be stated, never implied.
    const reason = guestLinkGateReason('http://127.0.0.1:8080', true)

    assert.match(String(reason), /HTTPS/)
  })

  test('a disabled capability keeps its own distinct reason', () => {
    // Two different kinds of "not covered" must stay distinguishable:
    // the deployment does not offer the feature, versus the feature
    // exists but this environment cannot exercise it.
    const disabled = guestLinkGateReason('https://inqtrix.example.test', false)

    assert.match(String(disabled), /not enabled/)
    assert.equal(/HTTPS/.test(String(disabled)), false)
  })

  test('a missing capability on an HTTP origin still reports the capability first', () => {
    const reason = guestLinkGateReason('http://127.0.0.1:8080', false)

    assert.match(String(reason), /not enabled/)
  })
})

function loadFixture(
  fixture: unknown,
  modes: { ownerMode?: number } = {},
): {
  configuration: ReturnType<typeof loadCollaborationE2EConfiguration>
  environment: Record<string, string>
} {
  const directory = mkdtempSync(join(tmpdir(), 'inqtrix-e2e-config-'))
  writeFileSync(join(directory, 'owner.json'), '{}', {
    mode: modes.ownerMode ?? 0o600,
  })
  writeFileSync(join(directory, 'collaborator.json'), '{}', { mode: 0o600 })
  writeFileSync(join(directory, 'fixture.json'), JSON.stringify(fixture))
  const environment = {
    INQTRIX_E2E_CONTROL_TOKEN: 'test-only-control-token',
    INQTRIX_E2E_FIXTURE: 'fixture.json',
    INQTRIX_E2E_MODE: 'strict',
  }
  return {
    configuration: loadCollaborationE2EConfiguration(environment, directory),
    environment,
  }
}

function validFixture() {
  return {
    controls: {
      armGatewayOutagePath: '/v1/test/collaboration/gateway-outage:arm',
      armLostAckPath: '/v1/test/collaboration/lost-ack:arm',
      armOutagePath: '/v1/test/collaboration/outage:arm',
      authorizationEnv: 'INQTRIX_E2E_CONTROL_TOKEN',
      baseURL: 'https://control.example.test',
      operationStatusPath: '/v1/test/collaboration/operation:status',
      restartPath: '/v1/test/collaboration/restart',
      restorePath: '/v1/test/collaboration/restore',
    },
    documents: {
      concurrent: 'concurrent-document',
      detachedTransfer: 'detached-transfer-document',
      directEdit: 'direct-document',
      downgrade: 'downgrade-document',
      gatewayOutage: 'gateway-outage-document',
      largeState: 'large-state-document',
      outage: 'outage-document',
      protocol: 'protocol-document',
      reconciliation: 'reconciliation-document',
      revocation: 'revocation-document',
      suggestion: {
        documentId: 'suggestion-document',
        expectedAuthorId: '00000000-0000-4000-8000-000000000002',
        expectedPermission: 'suggest',
      },
    },
    locale: 'en',
    privateAnchors: {
      collaborator: {
        aiAnchorText: 'collaborator-ai-anchor',
        aiText: 'collaborator-ai-marker',
        commentAnchorText: 'collaborator-comment-anchor',
        commentText: 'collaborator-comment-marker',
      },
      documents: {
        'chromium-desktop': 'private-anchor-chromium-desktop',
        'chromium-mobile': 'private-anchor-chromium-mobile',
        'firefox-desktop': 'private-anchor-firefox-desktop',
        'webkit-desktop': 'private-anchor-webkit-desktop',
      },
      owner: {
        aiAnchorText: 'owner-ai-anchor',
        aiText: 'owner-ai-marker',
        commentAnchorText: 'owner-comment-anchor',
        commentText: 'owner-comment-marker',
      },
    },
    transports: {
      nginx: { baseURL: 'https://nginx.example.test' },
      'python-gateway': {
        baseURL: 'https://python-gateway.example.test',
      },
      vite: { baseURL: 'https://vite.example.test' },
    },
    users: {
      collaborator: {
        displayName: 'Collaborator User',
        storageState: './collaborator.json',
        userId: '00000000-0000-4000-8000-000000000002',
      },
      owner: {
        displayName: 'Owner User',
        storageState: './owner.json',
        userId: '00000000-0000-4000-8000-000000000001',
      },
    },
    version: 2,
  }
}
