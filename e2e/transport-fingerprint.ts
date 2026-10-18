import type { APIRequestContext } from '@playwright/test'

import type { CollaborationTransport } from './config.ts'

export type TransportObservation = {
  rootContentType: string
  rootStatus: number
  serverHeader: string
  viteClientContentType: string
  viteClientMarker: boolean
  viteClientStatus: number
}

export async function observeTransportFingerprint(
  request: APIRequestContext,
  baseURL: string,
): Promise<TransportObservation> {
  const rootURL = normalizedBaseURL(baseURL)
  const viteClientURL = new URL('@vite/client', rootURL)
  const [rootResponse, viteClientResponse] = await Promise.all([
    request.get(rootURL.toString(), { failOnStatusCode: false, maxRedirects: 0 }),
    request.get(viteClientURL.toString(), { failOnStatusCode: false, maxRedirects: 0 }),
  ])
  const viteClientBody = await viteClientResponse.text()
  const viteClientContentType = header(viteClientResponse.headers(), 'content-type')
  return {
    rootContentType: header(rootResponse.headers(), 'content-type'),
    rootStatus: rootResponse.status(),
    serverHeader: header(rootResponse.headers(), 'server'),
    viteClientContentType,
    viteClientMarker: (
      isSuccess(viteClientResponse.status())
      && isJavaScript(viteClientContentType)
      && viteClientBody.includes('createHotContext')
    ),
    viteClientStatus: viteClientResponse.status(),
  }
}

export function assertTransportFingerprint(
  expected: CollaborationTransport,
  observation: TransportObservation,
): CollaborationTransport {
  const commonFailures: string[] = []
  if (!isSuccess(observation.rootStatus)) {
    commonFailures.push(`root returned HTTP ${observation.rootStatus}`)
  }
  if (!observation.rootContentType.includes('text/html')) {
    commonFailures.push(`root content-type was ${display(observation.rootContentType)}`)
  }

  const server = observation.serverHeader.toLowerCase()
  const specificFailures: string[] = []
  if (expected === 'vite') {
    if (!observation.viteClientMarker) {
      specificFailures.push(
        `Vite client marker absent (HTTP ${observation.viteClientStatus}, content-type ${display(observation.viteClientContentType)})`,
      )
    }
  } else if (expected === 'nginx') {
    if (!/^nginx(?:\/|$)/.test(server)) {
      specificFailures.push(`Server header was ${display(observation.serverHeader)}, expected nginx`)
    }
    if (observation.viteClientMarker) specificFailures.push('unexpected Vite client marker')
  } else {
    if (!/^uvicorn(?:\/|$)/.test(server)) {
      specificFailures.push(`Server header was ${display(observation.serverHeader)}, expected uvicorn`)
    }
    if (observation.viteClientMarker) specificFailures.push('unexpected Vite client marker')
  }

  const failures = [...commonFailures, ...specificFailures]
  if (failures.length > 0) {
    throw new Error(
      `Observable ${expected} transport fingerprint failed: ${failures.join('; ')}.`,
    )
  }
  return expected
}

function normalizedBaseURL(value: string): URL {
  const url = new URL(value)
  if (!url.pathname.endsWith('/')) url.pathname += '/'
  return url
}

function header(headers: Record<string, string>, name: string): string {
  return headers[name]?.trim().toLowerCase() ?? ''
}

function isSuccess(status: number): boolean {
  return status >= 200 && status < 300
}

function isJavaScript(contentType: string): boolean {
  return contentType.includes('javascript') || contentType.includes('ecmascript')
}

function display(value: string): string {
  return value || '<missing>'
}
