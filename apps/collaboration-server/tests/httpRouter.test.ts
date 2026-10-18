import type { IncomingMessage, ServerResponse } from 'node:http'

import type { LogFields, SidecarLogger } from '../src/contracts'
import { CloseCodes, CollaborationError } from '../src/errors'
import { correlationField, InternalHttpRouter } from '../src/httpRouter'
import type { InstanceLeaseManager } from '../src/instanceLease'
import { SidecarMetrics } from '../src/metrics'
import type { CollaborationOperations } from '../src/operations'
import { settings } from './helpers'

function request(headers: IncomingMessage['headers']): IncomingMessage {
  return { headers } as IncomingMessage
}

describe('correlationField: a refusal must be traceable to one click', () => {
  // One click produces log lines in three places: gateway, sidecar and
  // internal API. Without an id passed along they can only be matched by
  // timestamps, and not at all for two users at the same time.
  it('passes the caller\'s id on as a log field', () => {
    expect(correlationField(request({ 'x-request-id': 'abc123' })))
      .toEqual({ request_id: 'abc123' })
  })

  // An empty id is worse than none: it gathers every uncorrelated line under
  // one value and so claims a connection that does not exist.
  it('returns nothing rather than an empty id', () => {
    expect(correlationField(request({}))).toEqual({})
    expect(correlationField(request({ 'x-request-id': '' }))).toEqual({})
    expect(correlationField(request({ 'x-request-id': '   ' }))).toEqual({})
  })

  it('caps the length, because the header comes from the network', () => {
    const field = correlationField(request({ 'x-request-id': 'a'.repeat(500) }))

    expect('request_id' in field).toBe(true)
    expect((field as { request_id: string }).request_id).toHaveLength(128)
  })

  // Node does not drop repeated headers of this name; it joins them with
  // ", " into one string. An array built by hand would test a shape HTTP
  // never produces.
  it('takes the first value of joined headers', () => {
    expect(correlationField(request({ 'x-request-id': '7f3ac1, mesh-9942' })))
      .toEqual({ request_id: '7f3ac1' })
  })

  it('still accepts the array form, should a server deliver it', () => {
    expect(correlationField(request({ 'x-request-id': ['first', 'second'] })))
      .toEqual({ request_id: 'first' })
  })
})

describe('a refused suggestion publication names the rule that refused it', () => {
  // The answer said only "unsupported_suggestion_structure", and the log the
  // same: which rule refused the AI's new paragraph could be found out only
  // by reproducing the call.
  it('logs the suggestion reason and returns it next to the fixed reason', async () => {
    const warnings: Array<{ event: string; fields: LogFields | undefined }> = []
    const logger: SidecarLogger = {
      debug: () => undefined,
      error: () => undefined,
      info: () => undefined,
      warn: (event, fields) => {
        warnings.push({ event, fields })
      },
    }
    const metrics = new SidecarMetrics()
    const operations = {
      publishSuggestion: async () => {
        throw new CollaborationError('unsupported_suggestion_structure', {
          closeCode: CloseCodes.incompatible,
          httpStatus: 409,
          suggestionReason: 'node_marks_unsupported',
        })
      },
    } as unknown as CollaborationOperations
    const testSettings = settings()
    const router = new InternalHttpRouter(
      testSettings,
      {} as InstanceLeaseManager,
      operations,
      metrics,
      logger,
      () => true,
    )
    const body = Buffer.from(JSON.stringify({ patch_id: 'p' }))
    const incoming = {
      headers: {
        authorization: `Bearer ${testSettings.secret}`,
        'content-length': String(body.length),
        'content-type': 'application/json',
      },
      method: 'POST',
      url: '/internal/documents/doc-1/suggestions',
      async *[Symbol.asyncIterator]() {
        yield body
      },
    } as unknown as IncomingMessage
    const answer: { body: string; status: number } = { body: '', status: 0 }
    const response = {
      end: (text: string) => {
        answer.body = text
      },
      writeHead: (status: number) => {
        answer.status = status
      },
    } as unknown as ServerResponse

    await router.handle(incoming, response)

    expect(answer.status).toBe(409)
    expect(JSON.parse(answer.body)).toEqual({
      error: {
        reason: 'unsupported_suggestion_structure',
        suggestion_reason: 'node_marks_unsupported',
      },
    })
    expect(warnings).toEqual([{
      event: 'internal_http_operation_rejected',
      fields: {
        operation: 'suggestions',
        reason: 'unsupported_suggestion_structure',
        suggestion_reason: 'node_marks_unsupported',
      },
    }])
    // The metric keeps the fixed vocabulary; the open set of causes stays
    // out of its labels.
    expect(metrics.render()).not.toContain('node_marks_unsupported')
  })
})

describe('a request body may be as large as the documents it carries', () => {
  // A document of a JSON body can take up to twice its size once its line
  // breaks and quotes are escaped. A suggestion publication carries two
  // documents, its target and the base it was built on, and the API accepts
  // each of them up to the document limit; refusing such a body as too
  // large would refuse documents that are not.
  const LIMIT = 128 * 1024
  const SLACK = 64 * 1024

  async function post(
    path: string,
    body: Buffer,
  ): Promise<{ reached: string[]; status: number; text: string }> {
    const reached: string[] = []
    const operations = {
      project: async () => {
        reached.push('project')
        return {}
      },
      publishSuggestion: async () => {
        reached.push('suggestions')
        return {}
      },
    } as unknown as CollaborationOperations
    const testSettings = settings({ documentLimitBytes: LIMIT })
    const silent: SidecarLogger = {
      debug: () => undefined,
      error: () => undefined,
      info: () => undefined,
      warn: () => undefined,
    }
    const router = new InternalHttpRouter(
      testSettings,
      {} as InstanceLeaseManager,
      operations,
      new SidecarMetrics(),
      silent,
      () => true,
    )
    const incoming = {
      headers: {
        authorization: `Bearer ${testSettings.secret}`,
        'content-length': String(body.length),
        'content-type': 'application/json',
      },
      method: 'POST',
      url: path,
      async *[Symbol.asyncIterator]() {
        yield body
      },
    } as unknown as IncomingMessage
    const answer = { status: 0, text: '' }
    const response = {
      end: (text: string) => {
        answer.text = text
      },
      writeHead: (status: number) => {
        answer.status = status
      },
    } as unknown as ServerResponse
    await router.handle(incoming, response)
    return { reached, ...answer }
  }

  /** A valid JSON body of exactly `length` bytes. */
  const paddedBody = (length: number): Buffer => {
    const json = '{"patch_id":"p"}'
    return Buffer.from(json + ' '.repeat(length - json.length))
  }

  it('takes a publication whose target and base each stay within the document limit', async () => {
    // Short paragraphs: every "\n" becomes two bytes in the body.
    const document = 'a\n\n'.repeat(Math.floor(LIMIT / 3))
    expect(Buffer.byteLength(document)).toBeLessThanOrEqual(LIMIT)
    const body = Buffer.from(JSON.stringify({ base_markdown: document, target_markdown: document }))
    expect(body.length).toBeGreaterThan(2 * LIMIT + SLACK)

    const answer = await post('/internal/documents/doc-1/suggestions', body)

    expect(answer.status).toBe(200)
    expect(answer.reached).toEqual(['suggestions'])
  })

  it('bounds a publication at two documents and every other request at one', async () => {
    const publication = '/internal/documents/doc-1/suggestions'
    const projection = '/internal/documents/doc-1/project'
    const tooLarge = JSON.stringify({ error: { reason: 'document_too_large' } })

    expect((await post(publication, paddedBody(4 * LIMIT + SLACK))).reached).toEqual(['suggestions'])
    expect(await post(publication, paddedBody(4 * LIMIT + SLACK + 1)))
      .toEqual({ reached: [], status: 413, text: tooLarge })
    expect((await post(projection, paddedBody(2 * LIMIT + SLACK))).reached).toEqual(['project'])
    expect(await post(projection, paddedBody(2 * LIMIT + SLACK + 1)))
      .toEqual({ reached: [], status: 413, text: tooLarge })
  })
})
