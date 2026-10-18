import assert from 'node:assert/strict'
import {
  chmodSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from 'node:fs'
import { tmpdir } from 'node:os'
import { delimiter, join } from 'node:path'
import { describe, test } from 'node:test'

import {
  containerEnginePreflight,
  containerResourceNames,
  runContainerCommand,
} from './container-engine.ts'
import { createRunContext } from './run-context.ts'

describe('edge container-engine boundary', () => {
  test('requires an explicit engine and reports the opt-in as blocked', async () => {
    const repositoryRoot = mkdtempSync(join(tmpdir(), 'inqtrix-edge-preflight-'))
    try {
      const context = await createRunContext({
        profile: 'edge-conformance',
        repositoryRoot,
        runId: 'inqv-edge-preflight-01',
      })
      const checks = containerEnginePreflight(
        context,
        'web-edge-containers',
      )
      assert.equal(checks.length, 1)
      assert.equal(checks[0]?.status, 'failed')
      assert.match(checks[0]?.message ?? '', /opt-in/)
    } finally {
      rmSync(repositoryRoot, { force: true, recursive: true })
    }
  })

  test('distinguishes an unavailable daemon from engine selection', async () => {
    if (process.platform === 'win32') return
    const repositoryRoot = mkdtempSync(join(tmpdir(), 'inqtrix-edge-daemon-'))
    const binaryDirectory = join(repositoryRoot, 'bin')
    const executable = join(binaryDirectory, 'podman')
    try {
      await import('node:fs/promises').then(async ({ mkdir }) => {
        await mkdir(binaryDirectory, { recursive: true })
      })
      writeFileSync(
        executable,
        '#!/bin/sh\n[ "$1" = "version" ] && exit 0\nexit 23\n',
        'utf8',
      )
      chmodSync(executable, 0o700)
      const context = await createRunContext({
        containerEngine: 'podman',
        environment: {
          PATH: `${binaryDirectory}${delimiter}${process.env.PATH ?? ''}`,
        },
        profile: 'edge-conformance',
        repositoryRoot,
        runId: 'inqv-edge-daemon-0001',
      })
      const checks = containerEnginePreflight(
        context,
        'web-edge-containers',
      )
      assert.equal(
        checks.find((check) => check.id === 'container-engine-selected')?.status,
        'passed',
      )
      assert.equal(
        checks.find((check) => check.id === 'container-engine-daemon')?.status,
        'failed',
      )
    } finally {
      rmSync(repositoryRoot, { force: true, recursive: true })
    }
  })

  test('passes container arguments as argv without shell interpretation', async () => {
    if (process.platform === 'win32') return
    const directory = mkdtempSync(join(tmpdir(), 'inqtrix-edge-argv-'))
    const executable = join(directory, 'podman')
    const trace = join(directory, 'argv.txt')
    const shellMarker = join(directory, 'must-not-exist')
    try {
      writeFileSync(
        executable,
        '#!/bin/sh\nprintf "<%s>\\n" "$@" > "$INQTRIX_ARGV_TRACE"\n',
        'utf8',
      )
      chmodSync(executable, 0o700)
      const result = await runContainerCommand(
        'podman',
        ['run', `literal;touch ${shellMarker}`, '--label', 'safe=value'],
        {
          cwd: directory,
          environment: {
            INQTRIX_ARGV_TRACE: trace,
            PATH: `${directory}${delimiter}${process.env.PATH ?? ''}`,
          },
        },
      )
      assert.equal(result.exitCode, 0)
      assert.equal(
        readFileSync(trace, 'utf8'),
        `<run>\n<literal;touch ${shellMarker}>\n<--label>\n<safe=value>\n`,
      )
      assert.throws(() => readFileSync(shellMarker))
    } finally {
      rmSync(directory, { force: true, recursive: true })
    }
  })

  test('derives only run-bound resource names and labels', () => {
    const names = containerResourceNames('inqv-edge-contract-0001')
    assert.equal(
      names.label,
      'io.inqtrix.verification.run=inqv-edge-contract-0001',
    )
    for (const value of Object.values(names)) {
      assert.doesNotMatch(value, /\.\.|[/*?]/)
    }
  })

  test('uses normalized nginx locations and a named guest SPA fallback', () => {
    const template = readFileSync(
      new URL('../../deploy/nginx/inqtrix.conf.template', import.meta.url),
      'utf8',
    )
    assert.doesNotMatch(template, /map \$request_uri/)
    assert.match(template, /location ~ \^\/s\(\?:\/\|\$\)/)
    assert.match(template, /try_files \$uri @inqtrix_guest_spa;/)
    assert.match(template, /location @inqtrix_guest_spa/)
    assert.match(template, /\/v1\/editor\/share-links\/\[REDACTED\]/)
    assert.match(template, /location = \/readyz/)
    assert.match(template, /map \$http_connection \$inqtrix_invalid_connection_options/)
    const proxyPolicy = readFileSync(
      new URL('../../deploy/nginx/proxy-common.conf', import.meta.url),
      'utf8',
    )
    assert.match(proxyPolicy, /if \(\$inqtrix_invalid_connection_options\)/)
    assert.match(proxyPolicy, /proxy_set_header Trailer "";/)
    assert.doesNotMatch(proxyPolicy, /X-Inqtrix-Hop-Audit/)
  })

  test('never introduces broad cleanup or private env-file inputs', () => {
    const source = readFileSync(
      new URL('./adapters/web-edge-containers.ts', import.meta.url),
      'utf8',
    )
    assert.doesNotMatch(source, /\bprune\b/)
    assert.doesNotMatch(source, /--env-file|\.env\.(?:stack|secrets)/)
    assert.match(source, /label=\$\{this\.resources\.label\}/)
    const engineBoundary = readFileSync(
      new URL('./container-engine.ts', import.meta.url),
      'utf8',
    )
    assert.doesNotMatch(engineBoundary, /stdio:\s*['"]inherit['"]/)
    assert.doesNotMatch(engineBoundary, /capture:\s*false/)
    assert.doesNotMatch(engineBoundary, /\.\.\.process\.env/)
  })
})
