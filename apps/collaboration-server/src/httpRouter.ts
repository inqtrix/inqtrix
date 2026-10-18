import { Buffer } from 'node:buffer'
import { timingSafeEqual } from 'node:crypto'
import type { IncomingMessage, ServerResponse } from 'node:http'

import type { CollaborationSettings, SidecarLogger } from './contracts'
import {
  CloseCodes,
  CollaborationError,
  collaborationError,
} from './errors'
import { InstanceLeaseManager } from './instanceLease'
import { SidecarMetrics } from './metrics'
import { CollaborationOperations } from './operations'

const DOCUMENT_ROUTE = /^\/internal\/documents\/([A-Za-z0-9][A-Za-z0-9_-]{0,127})\/(decisions|project|suggestions)$/

export class InternalHttpRouter {
  constructor(
    private readonly settings: CollaborationSettings,
    private readonly leaseManager: InstanceLeaseManager,
    private readonly operations: CollaborationOperations,
    private readonly metrics: SidecarMetrics,
    private readonly logger: SidecarLogger,
    private readonly isReady: () => boolean = () => this.leaseManager.isReady(),
  ) {}

  async handle(request: IncomingMessage, response: ServerResponse): Promise<void> {
    const path = new URL(request.url ?? '/', 'http://collaboration.internal').pathname
    if (request.method === 'GET' && path === '/health/live') {
      this.json(response, 200, { status: 'alive' })
      return
    }
    if (request.method === 'GET' && path === '/health/ready') {
      const ready = this.isReady()
      this.json(response, ready ? 200 : 503, {
        mode: 'single_replica',
        protocol_version: this.settings.protocolVersion,
        schema_version: this.settings.schemaVersion,
        status: ready ? 'ready' : 'not_ready',
      })
      return
    }
    if (request.method === 'GET' && path === '/metrics') {
      response.writeHead(200, {
        'Cache-Control': 'no-store',
        'Content-Type': 'text/plain; version=0.0.4; charset=utf-8',
      })
      response.end(this.metrics.render())
      return
    }

    if (!hasBearerSecret(request.headers.authorization, this.settings.secret)) {
      this.metrics.increment('inqtrix_collaboration_http_rejections_total', { reason: 'unauthorized' })
      this.json(response, 401, { error: { reason: 'unauthorized' } })
      return
    }
    if (!this.isReady()) {
      this.metrics.increment('inqtrix_collaboration_http_rejections_total', {
        reason: 'service_unavailable',
      })
      this.json(response, 503, { error: { reason: 'service_unavailable' } })
      return
    }
    if (request.method !== 'POST') {
      this.json(response, 405, { error: { reason: 'method_not_allowed' } })
      return
    }

    const operation = path === '/internal/convert'
      ? 'convert'
      : DOCUMENT_ROUTE.test(path)
        ? path.endsWith('/decisions')
          ? 'decisions'
          : path.endsWith('/suggestions')
            ? 'suggestions'
            : 'project'
        : 'unknown'
    if (operation === 'unknown') {
      this.json(response, 404, { error: { reason: 'not_found' } })
      return
    }

    const startedAt = performance.now()
    try {
      // A suggestion publication carries two documents: its target and the
      // base it was built on. Every other request carries at most one.
      const payload = await this.readJson(request, operation === 'suggestions' ? 2 : 1)
      const route = DOCUMENT_ROUTE.exec(path)
      const result = operation === 'convert'
        ? await this.operations.convert(payload)
        : operation === 'decisions'
          ? await this.operations.decide(route![1]!, payload)
          : operation === 'suggestions'
            ? await this.operations.publishSuggestion(route![1]!, payload)
            : await this.operations.project(route![1]!, payload)
      this.metrics.increment('inqtrix_collaboration_http_requests_total', {
        operation,
        status: 'success',
      })
      this.metrics.observeMilliseconds(
        'inqtrix_collaboration_http_request_seconds',
        performance.now() - startedAt,
      )
      this.json(response, 200, result)
    } catch (error) {
      const mapped = collaborationError(error)
      this.metrics.increment('inqtrix_collaboration_http_requests_total', {
        operation,
        status: mapped.reason,
      })
      // The original reason must reach the log and the answer, or neither
      // the operator nor the user can find out why a request was refused.
      const upstream = mapped.upstreamReason && mapped.upstreamReason !== mapped.reason
        ? { upstream_reason: mapped.upstreamReason }
        : {}
      // Which rule refused a suggestion publication. The metric keeps the
      // fixed reason; the open set of causes belongs in the log and answer.
      const suggestion = mapped.suggestionReason
        ? { suggestion_reason: mapped.suggestionReason }
        : {}
      // The caller's correlation id, so that a refusal here and the line in
      // the API log are provably the same event rather than roughly the same
      // second. Without one the field is left out: an empty id would be
      // worse than none, because it joins what does not belong together.
      const correlation = correlationField(request)
      if (mapped.httpStatus >= 500) {
        this.logger.error('internal_http_operation_failed', {
          operation,
          reason: mapped.reason,
          ...correlation,
          ...upstream,
          ...suggestion,
        })
      } else {
        this.logger.warn('internal_http_operation_rejected', {
          operation,
          reason: mapped.reason,
          ...correlation,
          ...upstream,
          ...suggestion,
        })
      }
      this.json(response, mapped.httpStatus, {
        error: { reason: mapped.reason, ...upstream, ...suggestion },
      })
    }
  }

  /** Reads a JSON body that carries up to `documents` documents. Each may
   * take twice its size in the body once its line breaks and quotes are
   * escaped; the rest of a request fits in the fixed slack. */
  private async readJson(request: IncomingMessage, documents: 1 | 2): Promise<unknown> {
    const contentType = request.headers['content-type']?.split(';', 1)[0]?.trim().toLowerCase()
    if (contentType !== 'application/json') {
      throw invalidRequest()
    }
    const maximum = this.settings.documentLimitBytes * 2 * documents + 64 * 1024
    const declared = Number(request.headers['content-length'] ?? 0)
    if (Number.isFinite(declared) && declared > maximum) {
      throw requestTooLarge()
    }
    const chunks: Buffer[] = []
    let length = 0
    for await (const chunk of request) {
      const buffer = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk)
      length += buffer.length
      if (length > maximum) {
        throw requestTooLarge()
      }
      chunks.push(buffer)
    }
    try {
      return JSON.parse(Buffer.concat(chunks).toString('utf8')) as unknown
    } catch {
      throw invalidRequest()
    }
  }

  private json(response: ServerResponse, status: number, payload: unknown): void {
    response.writeHead(status, {
      'Cache-Control': 'no-store',
      'Content-Type': 'application/json; charset=utf-8',
    })
    response.end(JSON.stringify(payload))
  }
}

export function hasBearerSecret(
  authorization: string | string[] | undefined,
  secret: string,
): boolean {
  if (typeof authorization !== 'string') return false
  const actualBuffer = Buffer.from(authorization, 'utf8')
  const expectedBuffer = Buffer.from(`Bearer ${secret}`, 'utf8')
  return actualBuffer.length === expectedBuffer.length
    && timingSafeEqual(actualBuffer, expectedBuffer)
}

function invalidRequest(): CollaborationError {
  return new CollaborationError('invalid_request', {
    closeCode: CloseCodes.incompatible,
    httpStatus: 400,
  })
}

function requestTooLarge(): CollaborationError {
  return new CollaborationError('document_too_large', {
    closeCode: CloseCodes.messageTooLarge,
    httpStatus: 413,
  })
}

/** The caller's correlation id as a log field -- or nothing at all.
 *
 * Without a forwarded id, the log lines of one click can only be matched by
 * timestamp, and with two simultaneous users not at all. The API sets the
 * header in `_correlation_headers`; it is read here.
 *
 * This connects TWO stations, gateway and sidecar. This service's path back
 * to the internal API does NOT carry the id further -- see the docstring on
 * the Python side. The gap is named, not overlooked.
 *
 * A missing or empty header yields NO field. An empty id would be worse than
 * none: it would gather every uncorrelated line under the same value and so
 * claim a connection that does not exist. */
export function correlationField(
  request: IncomingMessage,
): { request_id: string } | Record<string, never> {
  const raw = request.headers['x-request-id']
  // Node does not drop repeated headers of this name; it joins them with
  // ", " into one string, so an array never arrives here. When an ingress
  // appends its own id to the caller's, the value would read like
  // "7f3a…c1, mesh-9942", which matches no log line: the correlation would
  // be silently wrong instead of simply absent. The first value counts,
  // because it comes from the original caller.
  const joined = Array.isArray(raw) ? raw[0] : raw
  const first = typeof joined === 'string' ? joined.split(',', 1)[0] : undefined
  const trimmed = first?.trim() ?? ''
  // Capped, because the header comes from the network and would otherwise
  // reach the log unbounded; the gateway's id is a 32-character hex string.
  return trimmed ? { request_id: trimmed.slice(0, 128) } : {}
}
