import { request as httpRequest } from 'node:http'
import { setTimeout as delay } from 'node:timers/promises'

import type { VerificationAdapter } from '../adapter.ts'
import type { CleanupLedger } from '../cleanup-ledger.ts'
import {
  containerEnginePreflight,
  containerResourceNames,
  requireContainerCommand,
  runContainerCommand,
  type ContainerCommandResult,
  type ContainerResourceNames,
} from '../container-engine.ts'
import type {
  EngineResult,
  ScenarioExecutionResult,
} from '../model.ts'
import {
  repositoryFileCheck,
} from '../preflight.ts'
import type { RunContext } from '../run-context.ts'

const ENGINE = 'web-edge-containers' as const
const EDGE_PORT = 8080
const BACKEND_PORT = 5100
const BODY_LIMIT_BYTES = 64
const START_TIMEOUT_MS = 30_000
const REQUEST_TIMEOUT_MS = 5_000
const SYNTHETIC_BACKEND_SOURCE = `
import asyncio
import uvicorn
from fastapi import FastAPI, Request, Response, WebSocket
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.get("/readyz")
async def readyz(degraded: bool = False):
    payload = {
        "status": "not_ready" if degraded else "ready",
        "checks": {
            "database": "error" if degraded else "ok",
            "queue": "ok",
            "vector_store": "ok",
            "object_store": "ok",
        },
    }
    return JSONResponse(payload, status_code=503 if degraded else 200)

@app.post("/api/echo")
async def echo(request: Request):
    body = await request.body()
    return {"bytes": len(body)}

@app.get("/api/cookies")
async def cookies():
    response = Response("cookies", media_type="text/plain")
    response.headers.append("set-cookie", "first=one; Path=/; HttpOnly")
    response.headers.append("set-cookie", "second=two; Path=/; SameSite=Lax")
    return response

@app.get("/api/sse")
async def sse():
    async def events():
        yield b"data: first\\n\\n"
        await asyncio.sleep(0.5)
        yield b"data: second\\n\\n"
    return StreamingResponse(events(), media_type="text/event-stream")

@app.api_route("/api/hop", methods=["GET", "POST"])
async def hop(request: Request):
    response = JSONResponse({
        "connection_nominated": request.headers.get("x-inqtrix-hop-audit"),
        "trailer": request.headers.get("trailer"),
    })
    response.headers["connection"] = "close"
    response.headers["keep-alive"] = "timeout=5"
    response.headers["trailer"] = "X-Inqtrix-Later"
    return response

@app.api_route("/v1/editor/share-links", methods=["GET", "POST"])
@app.api_route("/v1/editor/share-links/{remainder:path}", methods=["GET", "POST"])
async def share_link(remainder: str = ""):
    return {"route": "share-link", "remainder": remainder}

@app.websocket("/collaboration")
async def collaboration(websocket: WebSocket):
    await websocket.accept()
    if websocket.query_params.get("document") != "synthetic":
        await websocket.close(code=1008, reason="missing_document")
        return
    payload = await websocket.receive_bytes()
    await websocket.send_bytes(payload)
    await websocket.close(code=1000, reason="complete")

uvicorn.run(
    app,
    host="0.0.0.0",
    port=${BACKEND_PORT},
    access_log=False,
    log_level="warning",
)
`

type AdapterName = 'nginx' | 'python'

type EdgeEndpoint = {
  adapter: AdapterName
  container: string
  port: number
}

type EdgeResponse = {
  body: Buffer
  firstChunkMs: number | null
  headers: Map<string, string[]>
  status: number
  totalMs: number
}

type EdgeScenario = {
  id: string
  run(stack: WebEdgeStack): Promise<void>
}

const EDGE_SCENARIOS: readonly EdgeScenario[] = [
  {
    id: 'edge.static-spa-cache',
    run: verifyStaticSpaCache,
  },
  {
    id: 'edge.readiness-contract',
    run: verifyReadinessContract,
  },
  {
    id: 'edge.http-streaming-cookies',
    run: verifyStreamingAndCookies,
  },
  {
    id: 'edge.hop-by-hop-headers',
    run: verifyHopByHopHeaders,
  },
  {
    id: 'edge.request-body-limit',
    run: verifyRequestBodyLimit,
  },
  {
    id: 'edge.websocket-contract',
    run: verifyWebSocketContract,
  },
  {
    id: 'edge.guest-security-and-redaction',
    run: verifyGuestSecurityAndRedaction,
  },
  {
    id: 'edge.runtime-hardening',
    run: verifyRuntimeHardening,
  },
  {
    id: 'edge.backend-recovery',
    run: verifyBackendRecovery,
  },
]

export const webEdgeContainersAdapter: VerificationAdapter = {
  engine: ENGINE,
  profiles: ['edge-conformance'],
  async preflight(context) {
    return [
      repositoryFileCheck(
        context,
        ENGINE,
        'web-dockerfile',
        'deploy/docker/Dockerfile.web',
      ),
      repositoryFileCheck(
        context,
        ENGINE,
        'nginx-template',
        'deploy/nginx/inqtrix.conf.template',
      ),
      repositoryFileCheck(
        context,
        ENGINE,
        'nginx-static-headers',
        'deploy/nginx/static-headers.conf',
      ),
      repositoryFileCheck(
        context,
        ENGINE,
        'python-lock',
        'uv.lock',
      ),
      repositoryFileCheck(
        context,
        ENGINE,
        'javascript-lock',
        'package-lock.json',
      ),
      ...containerEnginePreflight(context, ENGINE),
    ]
  },
  async execute(context, cleanupLedger) {
    const startedAt = new Date()
    if (!context.containerEngine) {
      throw new Error('Container engine became unavailable after preflight.')
    }
    const stack = new WebEdgeStack(context, cleanupLedger)
    const scenarios: ScenarioExecutionResult[] = []
    let status: EngineResult['status'] = 'passed'
    try {
      await stack.start()
      for (const scenario of EDGE_SCENARIOS) {
        if (context.abortSignal.aborted) {
          status = 'interrupted'
          break
        }
        try {
          await scenario.run(stack)
          scenarios.push({ id: scenario.id, status: 'passed' })
        } catch (error) {
          scenarios.push({ id: scenario.id, status: 'failed' })
          status = 'failed'
          const message = error instanceof Error
            ? error.message
            : 'Unknown edge-conformance failure.'
          process.stderr.write(`[${scenario.id}] ${message}\n`)
        }
      }
    } catch {
      status = context.abortSignal.aborted ? 'interrupted' : 'failed'
    }
    const finishedAt = new Date()
    return {
      durationMs: Math.max(0, finishedAt.getTime() - startedAt.getTime()),
      engine: ENGINE,
      exitCode: status === 'passed' ? 0 : status === 'failed' ? 1 : null,
      finishedAt: finishedAt.toISOString(),
      scenarios,
      signal: status === 'interrupted' ? 'SIGTERM' : null,
      startedAt: startedAt.toISOString(),
      status,
    }
  },
}

class WebEdgeStack {
  readonly context: RunContext
  readonly resources: ContainerResourceNames
  private readonly cleanupLedger: CleanupLedger
  private readonly endpoints = new Map<AdapterName, EdgeEndpoint>()

  constructor(context: RunContext, cleanupLedger: CleanupLedger) {
    this.context = context
    this.cleanupLedger = cleanupLedger
    this.resources = containerResourceNames(context.runId)
  }

  async start(): Promise<void> {
    await this.cleanupLedger.register(
      'resource',
      'edge-conformance residual resource check',
      async () => await this.assertNoResidualResources(),
    )
    await this.registerImage(
      this.resources.pythonImage,
      'web-python',
    )
    await this.registerImage(
      this.resources.nginxImage,
      'web-nginx',
    )
    await this.registerNetwork()
    await this.registerContainer(
      this.resources.backendContainer,
      this.backendArgs(),
    )
    await this.registerContainer(
      this.resources.pythonContainer,
      this.edgeArgs('python'),
    )
    await this.registerContainer(
      this.resources.nginxContainer,
      this.edgeArgs('nginx'),
    )
    this.endpoints.set(
      'python',
      await this.resolveEndpoint('python', this.resources.pythonContainer),
    )
    this.endpoints.set(
      'nginx',
      await this.resolveEndpoint('nginx', this.resources.nginxContainer),
    )
    await Promise.all(this.allEndpoints().map(async (endpoint) => {
      await waitForStatus(endpoint, '/health', 200, START_TIMEOUT_MS)
    }))
  }

  allEndpoints(): EdgeEndpoint[] {
    return (['python', 'nginx'] as const).map(
      (adapter) => this.endpoint(adapter),
    )
  }

  endpoint(adapter: AdapterName): EdgeEndpoint {
    const endpoint = this.endpoints.get(adapter)
    if (!endpoint) throw new Error(`The ${adapter} edge is not running.`)
    return endpoint
  }

  async command(
    args: readonly string[],
    abortable = true,
  ): Promise<ContainerCommandResult> {
    const engine = this.context.containerEngine
    if (!engine) throw new Error('Container engine is not configured.')
    return await runContainerCommand(engine, args, {
      abortSignal: abortable ? this.context.abortSignal : undefined,
      cwd: this.context.repositoryRoot,
      environment: this.context.environment,
    })
  }

  async logs(endpoint: EdgeEndpoint): Promise<string> {
    const result = await this.command(['logs', endpoint.container])
    requireContainerCommand(result, `read ${endpoint.adapter} logs`)
    return `${result.stdout}\n${result.stderr}`
  }

  private async registerImage(tag: string, target: string): Promise<void> {
    await this.cleanupLedger.register(
      'resource',
      `${target} run image`,
      async () => {
        await this.command(['image', 'rm', '--force', tag], false)
      },
    )
    const result = await this.command([
      'build',
      '--label',
      this.resources.label,
      '--file',
      'deploy/docker/Dockerfile.web',
      '--target',
      target,
      '--tag',
      tag,
      '.',
    ])
    requireContainerCommand(result, `build ${target}`)
  }

  private async registerNetwork(): Promise<void> {
    await this.cleanupLedger.register(
      'resource',
      'edge-conformance isolated network',
      async () => {
        await this.command(
          ['network', 'rm', this.resources.network],
          false,
        )
      },
    )
    const result = await this.command([
      'network',
      'create',
      '--label',
      this.resources.label,
      this.resources.network,
    ])
    requireContainerCommand(result, 'create edge network')
  }

  private async registerContainer(
    name: string,
    args: readonly string[],
  ): Promise<void> {
    await this.cleanupLedger.register(
      'resource',
      `${name} container`,
      async () => {
        await this.command(['rm', '--force', name], false)
      },
    )
    const result = await this.command(args)
    requireContainerCommand(result, `start ${name}`)
  }

  private backendArgs(): string[] {
    return [
      'run',
      '--detach',
      '--name',
      this.resources.backendContainer,
      '--label',
      this.resources.label,
      '--network',
      this.resources.network,
      '--user',
      '1001:0',
      '--read-only',
      '--cap-drop=ALL',
      '--security-opt=no-new-privileges',
      '--tmpfs',
      '/tmp:rw,noexec,nosuid,size=64m',
      this.resources.pythonImage,
      'python',
      '-c',
      SYNTHETIC_BACKEND_SOURCE,
    ]
  }

  private edgeArgs(adapter: AdapterName): string[] {
    const container = adapter === 'python'
      ? this.resources.pythonContainer
      : this.resources.nginxContainer
    const image = adapter === 'python'
      ? this.resources.pythonImage
      : this.resources.nginxImage
    return [
      'run',
      '--detach',
      '--name',
      container,
      '--label',
      this.resources.label,
      '--network',
      this.resources.network,
      '--publish',
      `127.0.0.1::${EDGE_PORT}`,
      '--user',
      '1001:0',
      '--read-only',
      '--cap-drop=ALL',
      '--security-opt=no-new-privileges',
      '--tmpfs',
      '/tmp:rw,noexec,nosuid,size=64m',
      '--env',
      `INQTRIX_WEB_ADAPTER=${adapter}`,
      '--env',
      'INQTRIX_DIRECT_TLS=false',
      '--env',
      `INQTRIX_BACKEND_URL=http://${this.resources.backendContainer}:${BACKEND_PORT}`,
      '--env',
      `INQTRIX_MAX_FILE_BYTES=${BODY_LIMIT_BYTES}`,
      '--env',
      `INQTRIX_PROXY_MAX_BODY_BYTES=${BODY_LIMIT_BYTES}`,
      '--env',
      'NGINX_ENTRYPOINT_LOCAL_RESOLVERS=1',
      image,
    ]
  }

  private async resolveEndpoint(
    adapter: AdapterName,
    container: string,
  ): Promise<EdgeEndpoint> {
    const result = await this.command([
      'port',
      container,
      `${EDGE_PORT}/tcp`,
    ])
    requireContainerCommand(result, `resolve ${adapter} edge port`)
    const match = result.stdout
      .split(/\r?\n/)
      .map((line) => line.match(/:(\d+)\s*$/))
      .find((candidate) => candidate !== null)
    const port = match?.[1] ? Number(match[1]) : Number.NaN
    if (!Number.isInteger(port) || port < 1 || port > 65_535) {
      throw new Error(`The ${adapter} edge did not publish a valid port.`)
    }
    return { adapter, container, port }
  }

  private async assertNoResidualResources(): Promise<void> {
    const queries = [
      ['container', 'ls', '--all', '--quiet', '--filter', `label=${this.resources.label}`],
      ['network', 'ls', '--quiet', '--filter', `label=${this.resources.label}`],
      ['image', 'ls', '--quiet', '--filter', `label=${this.resources.label}`],
    ] as const
    for (const args of queries) {
      const result = await this.command(args, false)
      requireContainerCommand(result, 'verify edge resource cleanup')
      if (result.stdout.trim()) {
        throw new Error('Run-labelled edge resources remain after cleanup.')
      }
    }
  }
}

async function verifyStaticSpaCache(stack: WebEdgeStack): Promise<void> {
  for (const endpoint of stack.allEndpoints()) {
    const root = await edgeRequest(endpoint, '/')
    ensure(root.status === 200, 'The SPA root did not return 200.')
    ensure(header(root, 'cache-control') === 'no-cache', 'The SPA root cache policy drifted.')
    ensure(
      header(root, 'content-type')?.startsWith('text/html') ?? false,
      'The SPA root is not HTML.',
    )
    const deepLink = await edgeRequest(endpoint, '/settings/synthetic-deep-link')
    ensure(deepLink.status === 200, 'The SPA fallback did not return 200.')
    ensure(deepLink.body.equals(root.body), 'The SPA fallback did not serve index.html.')
    ensure(
      header(deepLink, 'cache-control') === 'no-cache',
      'The SPA fallback cache policy drifted.',
    )
    const assetReference = firstAssetReference(root.body.toString('utf8'))
    const asset = await edgeRequest(endpoint, assetReference)
    ensure(asset.status === 200, 'A built SPA asset was not served.')
    ensure(
      header(asset, 'cache-control') === 'public, max-age=31536000, immutable',
      'The immutable asset cache policy drifted.',
    )
    const missingAsset = await edgeRequest(
      endpoint,
      '/assets/inqtrix-edge-conformance-missing.js',
    )
    ensure(missingAsset.status === 404, 'A missing asset fell back to the SPA.')
  }
}

async function verifyReadinessContract(stack: WebEdgeStack): Promise<void> {
  for (const endpoint of stack.allEndpoints()) {
    const ready = await edgeRequest(endpoint, '/readyz')
    ensure(ready.status === 200, `${endpoint.adapter} did not relay ready=200.`)
    ensure(
      header(ready, 'content-type')?.startsWith('application/json') ?? false,
      `${endpoint.adapter} replaced readiness JSON with another content type.`,
    )
    const readyPayload = JSON.parse(ready.body.toString('utf8')) as {
      checks?: Record<string, string>
      status?: string
    }
    ensure(
      readyPayload.status === 'ready'
        && readyPayload.checks?.database === 'ok',
      `${endpoint.adapter} changed the ready dependency payload.`,
    )

    const degraded = await edgeRequest(endpoint, '/readyz?degraded=true')
    ensure(
      degraded.status === 503,
      `${endpoint.adapter} did not preserve backend readiness=503.`,
    )
    ensure(
      header(degraded, 'content-type')?.startsWith('application/json') ?? false,
      `${endpoint.adapter} replaced degraded readiness JSON.`,
    )
    const degradedPayload = JSON.parse(degraded.body.toString('utf8')) as {
      checks?: Record<string, string>
      status?: string
    }
    ensure(
      degradedPayload.status === 'not_ready'
        && degradedPayload.checks?.database === 'error',
      `${endpoint.adapter} changed the degraded dependency payload.`,
    )
  }
}

async function verifyStreamingAndCookies(stack: WebEdgeStack): Promise<void> {
  for (const endpoint of stack.allEndpoints()) {
    const cookies = await edgeRequest(endpoint, '/api/cookies')
    ensure(cookies.status === 200, 'The cookie fixture did not return 200.')
    ensure(
      (cookies.headers.get('set-cookie') ?? []).length === 2,
      'Duplicate Set-Cookie fields were collapsed.',
    )
    const sse = await edgeRequest(endpoint, '/api/sse')
    ensure(sse.status === 200, 'The SSE fixture did not return 200.')
    ensure(
      header(sse, 'content-type')?.startsWith('text/event-stream') ?? false,
      'The SSE content type drifted.',
    )
    ensure(
      sse.body.toString('utf8') === 'data: first\n\ndata: second\n\n',
      'The SSE payload drifted.',
    )
    ensure(
      sse.firstChunkMs !== null
        && sse.firstChunkMs < 450
        && sse.totalMs >= 450,
      'The edge buffered the complete SSE response.',
    )
  }
}

async function verifyHopByHopHeaders(stack: WebEdgeStack): Promise<void> {
  for (const endpoint of stack.allEndpoints()) {
    const nominated = await edgeRequest(endpoint, '/api/hop', {
      headers: {
        Connection: 'keep-alive, X-Inqtrix-Hop-Audit',
        'X-Inqtrix-Hop-Audit': 'must-not-cross',
      },
    })
    if (endpoint.adapter === 'nginx') {
      ensure(
        nominated.status === 400,
        'nginx did not fail closed on an unknown Connection option.',
      )
    } else {
      ensure(
        nominated.status === 200,
        'Python did not accept a removable Connection option.',
      )
      const observed = JSON.parse(nominated.body.toString('utf8')) as {
        connection_nominated?: string | null
      }
      ensure(
        observed.connection_nominated == null,
        'Python forwarded a Connection-nominated request field.',
      )
    }

    const response = await edgeRequest(endpoint, '/api/hop', {
      chunks: [],
      headers: {
        Connection: 'keep-alive',
        Trailer: 'X-Inqtrix-Later',
      },
      method: 'POST',
    })
    ensure(response.status === 200, 'The hop-header fixture did not return 200.')
    const observed = JSON.parse(response.body.toString('utf8')) as {
      trailer?: string | null
    }
    ensure(
      observed.trailer == null,
      `${endpoint.adapter} forwarded the Trailer request field.`,
    )
    ensure(
      !response.headers.has('keep-alive')
        && !response.headers.has('trailer'),
      `${endpoint.adapter} forwarded a response hop-by-hop field.`,
    )
  }
}

async function verifyRequestBodyLimit(stack: WebEdgeStack): Promise<void> {
  for (const endpoint of stack.allEndpoints()) {
    const exact = await edgeRequest(
      endpoint,
      '/api/echo',
      {
        chunks: [Buffer.alloc(31, 'a'), Buffer.alloc(33, 'b')],
        method: 'POST',
      },
    )
    ensure(exact.status === 200, 'A body at the exact edge limit was rejected.')
    ensure(
      JSON.parse(exact.body.toString('utf8')).bytes === BODY_LIMIT_BYTES,
      'The exact-limit body did not reach the backend intact.',
    )
    const oversized = await edgeRequest(
      endpoint,
      '/api/echo',
      {
        chunks: [Buffer.alloc(32, 'a'), Buffer.alloc(33, 'b')],
        method: 'POST',
      },
    )
    ensure(oversized.status === 413, 'A chunked oversized body bypassed the edge limit.')
    const recovery = await edgeRequest(endpoint, '/health')
    ensure(recovery.status === 200, 'The edge did not recover after rejecting a body.')
  }
}

async function verifyWebSocketContract(stack: WebEdgeStack): Promise<void> {
  for (const endpoint of stack.allEndpoints()) {
    await websocketRoundTrip(endpoint)
  }
}

async function verifyGuestSecurityAndRedaction(
  stack: WebEdgeStack,
): Promise<void> {
  const guestCases = [
    ['/s/edge-normal-guest', 'edge-normal-guest'],
    ['/s%2Fedge-encoded-separator-guest', 'edge-encoded-separator-guest'],
    ['/%73/edge-encoded-letter-guest', 'edge-encoded-letter-guest'],
  ] as const
  const shareCases = [
    [
      '/v1/editor/share-links/edge-normal-share',
      'edge-normal-share',
    ],
    [
      '/v1/editor/share-links%2Fedge-encoded-separator-share',
      'edge-encoded-separator-share',
    ],
    [
      '/v1/%65ditor/share-links/edge-encoded-letter-share',
      'edge-encoded-letter-share',
    ],
  ] as const
  for (const endpoint of stack.allEndpoints()) {
    for (const [path] of guestCases) {
      const response = await edgeRequest(endpoint, path)
      ensure(response.status === 200, 'A normalized guest SPA route did not return 200.')
      ensure(
        header(response, 'cache-control') === 'no-store'
          && header(response, 'referrer-policy') === 'no-referrer'
          && header(response, 'x-content-type-options') === 'nosniff',
        'A normalized guest route lost its privacy headers.',
      )
    }
    for (const [path] of shareCases) {
      const response = await edgeRequest(endpoint, path)
      ensure(response.status === 200, 'A normalized share-link API route was not proxied.')
    }
    await edgeRequest(
      endpoint,
      '/s/edge-query-guest?token=edge-query-marker',
    )
    const markers = [
      ...guestCases.map(([, marker]) => marker),
      ...shareCases.map(([, marker]) => marker),
      'edge-query-marker',
    ]
    const logs = await waitForRedactedLogs(stack, endpoint)
    ensure(
      logs.includes('/s/[REDACTED]')
        && logs.includes('/v1/editor/share-links/[REDACTED]'),
      'The edge emitted no affirmative redacted route evidence.',
    )
    const exposedMarker = markers.findIndex((marker) => logs.includes(marker))
    ensure(
      exposedMarker === -1,
      `${endpoint.adapter} access log exposed synthetic marker case ${exposedMarker + 1}.`,
    )
  }
}

async function verifyRuntimeHardening(stack: WebEdgeStack): Promise<void> {
  for (const endpoint of stack.allEndpoints()) {
    const identity = await stack.command([
      'exec',
      endpoint.container,
      'id',
      '-u',
    ])
    requireContainerCommand(identity, `inspect ${endpoint.adapter} identity`)
    ensure(identity.stdout.trim() === '1001', 'The edge is not running as UID 1001.')
    const write = await stack.command([
      'exec',
      endpoint.container,
      'touch',
      '/inqtrix-edge-conformance-write-test',
    ])
    ensure(write.exitCode !== 0, 'The edge root filesystem is writable.')
  }
}

async function verifyBackendRecovery(stack: WebEdgeStack): Promise<void> {
  const stop = await stack.command([
    'stop',
    '--time',
    '2',
    stack.resources.backendContainer,
  ])
  requireContainerCommand(stop, 'stop synthetic backend')
  for (const endpoint of stack.allEndpoints()) {
    await waitForStatus(endpoint, '/health', 502, 10_000)
  }
  const start = await stack.command([
    'start',
    stack.resources.backendContainer,
  ])
  requireContainerCommand(start, 'restart synthetic backend')
  for (const endpoint of stack.allEndpoints()) {
    await waitForStatus(endpoint, '/health', 200, START_TIMEOUT_MS)
  }
}

function edgeRequest(
  endpoint: EdgeEndpoint,
  path: string,
  options: {
    chunks?: readonly Buffer[]
    headers?: Readonly<Record<string, string>>
    method?: string
  } = {},
): Promise<EdgeResponse> {
  const started = Date.now()
  return new Promise<EdgeResponse>((resolveResponse, reject) => {
    const chunks: Buffer[] = []
    let firstChunkMs: number | null = null
    const request = httpRequest({
      headers: {
        ...(options.chunks ? { 'Transfer-Encoding': 'chunked' } : {}),
        ...options.headers,
      },
      host: '127.0.0.1',
      method: options.method ?? 'GET',
      path,
      port: endpoint.port,
      timeout: REQUEST_TIMEOUT_MS,
    }, (response) => {
      response.on('data', (chunk: Buffer) => {
        if (firstChunkMs === null) firstChunkMs = Date.now() - started
        chunks.push(Buffer.from(chunk))
      })
      response.once('end', () => {
        const headers = new Map<string, string[]>()
        for (let index = 0; index < response.rawHeaders.length; index += 2) {
          const name = response.rawHeaders[index]?.toLowerCase()
          const value = response.rawHeaders[index + 1]
          if (!name || value === undefined) continue
          headers.set(name, [...(headers.get(name) ?? []), value])
        }
        resolveResponse({
          body: Buffer.concat(chunks),
          firstChunkMs,
          headers,
          status: response.statusCode ?? 0,
          totalMs: Date.now() - started,
        })
      })
    })
    request.once('timeout', () => {
      request.destroy(new Error('Edge request timed out.'))
    })
    request.once('error', reject)
    for (const chunk of options.chunks ?? []) request.write(chunk)
    request.end()
  })
}

function websocketRoundTrip(endpoint: EdgeEndpoint): Promise<void> {
  return new Promise<void>((resolveRoundTrip, reject) => {
    const socket = new WebSocket(
      `ws://127.0.0.1:${endpoint.port}/collaboration?document=synthetic`,
    )
    const timeout = setTimeout(() => {
      socket.close()
      reject(new Error('WebSocket conformance request timed out.'))
    }, REQUEST_TIMEOUT_MS)
    let echoed = false
    socket.binaryType = 'arraybuffer'
    socket.addEventListener('open', () => {
      socket.send(new Uint8Array([0, 1, 2, 255]))
    })
    socket.addEventListener('message', (event) => {
      const payload = new Uint8Array(event.data as ArrayBuffer)
      echoed = payload.length === 4
        && payload[0] === 0
        && payload[1] === 1
        && payload[2] === 2
        && payload[3] === 255
    })
    socket.addEventListener('close', (event) => {
      clearTimeout(timeout)
      if (echoed && event.code === 1000) resolveRoundTrip()
      else reject(new Error('WebSocket binary echo or close code drifted.'))
    })
    socket.addEventListener('error', () => {
      clearTimeout(timeout)
      reject(new Error('WebSocket conformance request failed.'))
    })
  })
}

async function waitForStatus(
  endpoint: EdgeEndpoint,
  path: string,
  expectedStatus: number,
  timeoutMs: number,
): Promise<void> {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    try {
      const response = await edgeRequest(endpoint, path)
      if (response.status === expectedStatus) return
    } catch {
      // Bounded readiness/recovery polling intentionally tolerates transition
      // failures; the deadline remains authoritative.
    }
    await delay(200)
  }
  throw new Error(
    `${endpoint.adapter} did not reach HTTP ${expectedStatus} before timeout.`,
  )
}

async function waitForRedactedLogs(
  stack: WebEdgeStack,
  endpoint: EdgeEndpoint,
): Promise<string> {
  const deadline = Date.now() + REQUEST_TIMEOUT_MS
  let logs = ''
  while (Date.now() < deadline) {
    logs = await stack.logs(endpoint)
    if (
      logs.includes('/s/[REDACTED]')
      && logs.includes('/v1/editor/share-links/[REDACTED]')
    ) {
      return logs
    }
    await delay(100)
  }
  return logs
}

function firstAssetReference(indexHtml: string): string {
  const match = indexHtml.match(
    /\b(?:src|href)=["']([^"']*\/assets\/[^"']+)["']/,
  )
  if (!match?.[1]) {
    throw new Error('The built index contains no asset reference.')
  }
  return new URL(match[1], 'http://edge.invalid/').pathname
}

function header(response: EdgeResponse, name: string): string | undefined {
  return response.headers.get(name.toLowerCase())?.[0]
}

function ensure(condition: boolean, message: string): asserts condition {
  if (!condition) throw new Error(message)
}
