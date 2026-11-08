import { readFileSync } from 'node:fs'
import { createServer, type Server } from 'node:http'
import type { AddressInfo } from 'node:net'
import { inspect } from 'node:util'

import { Hocuspocus } from '@hocuspocus/server'
import { serializeEditorJson, setCitationLabelsLeftEscapedReporter } from '@inqtrix/editor-schema'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { ConfigError, logLevelFromEnv, startFailureFields } from '../src/config'
import { CollaborationError, HookRejection, collaborationError, quietHook, quietHooks } from '../src/errors'
import { consoleSink, createJsonLogger, routeEditorSchemaWarnings, routeLibraryConsole } from '../src/logger'
import { CollaborationSidecar } from '../src/sidecar'
import { VERIFICATION_FAULT_ENV, verificationFaultGateFromEnv } from '../src/verificationFault'
import { FakeCollaborationApi, settings, silentLogger } from './helpers'

const MARKER = 'zzmarker7'

/** The field contract the Python services share with this server. */
const contract = JSON.parse(
  readFileSync(new URL('../../../tests/fixtures/logging/field_contract.json', import.meta.url), 'utf8'),
) as { common_fields: string[]; levels: string[]; ts_pattern: string }

afterEach(() => {
  setCitationLabelsLeftEscapedReporter(null)
  vi.restoreAllMocks()
})

function libraryConsole() {
  const printed = { debug: vi.fn(), error: vi.fn(), info: vi.fn(), log: vi.fn(), warn: vi.fn() }
  const target = { ...printed }
  routeLibraryConsole(createJsonLogger(consoleSink(target)), target)
  const records = () => [printed.debug, printed.error, printed.info, printed.warn]
    .flatMap((method) => method.mock.calls.map(([line]) => JSON.parse(String(line))))
  return { printed, records, target }
}

describe('what a library prints past the logger', () => {
  const refusal = () => new HookRejection(
    new CollaborationError('access_revoked', { closeCode: 4403 }),
  )

  it('adds nothing for a connection a hook refused, in either form the library prints', () => {
    const { printed, target } = libraryConsole()
    target.error('closing connection a (while handling b) because of exception', refusal())
    target.error(refusal())
    expect(printed.error).not.toHaveBeenCalled()
  })

  it('makes one record of an unexpected error, with its frames and without its text below the first line', () => {
    const { records, target } = libraryConsole()
    const failure = new TypeError(`broken\n${MARKER}`)
    target.error('closing connection a (while handling b) because of exception', failure)
    const [record, ...rest] = records()
    expect(rest).toEqual([])
    expect(record).toMatchObject({
      error_type: 'TypeError',
      event: 'library_console',
      level: 'ERROR',
      service: 'collaboration',
    })
    expect(record.text).toBe('closing connection a (while handling b) because of exception TypeError: broken')
    expect(record.stack).toContain('logger.test.ts')
    expect(record.stack).not.toContain(MARKER)
  })

  it('keeps the level of the method the library called', () => {
    const { records, target } = libraryConsole()
    target.warn('a warning')
    target.log('a note')
    target.info('another note')
    expect(records().map(({ level, text }) => [level, text])).toEqual([
      ['INFO', 'a note'],
      ['INFO', 'another note'],
      ['WARNING', 'a warning'],
    ])
  })

  it('bounds the text and drops an empty line', () => {
    const { printed, records, target } = libraryConsole()
    target.log()
    expect(printed.info).not.toHaveBeenCalled()
    target.log('x'.repeat(500))
    expect(records()[0].text).toHaveLength(200)
  })
})

function sinkFor() {
  return { debug: vi.fn(), error: vi.fn(), info: vi.fn(), warn: vi.fn() }
}

function record(call: unknown[] | undefined): Record<string, unknown> {
  return JSON.parse(String(call?.[0])) as Record<string, unknown>
}

describe('json log records', () => {
  it('carry the fields of the shared contract', () => {
    const sink = sinkFor()
    createJsonLogger(sink).warn('instance_lease_unavailable', { room: 'r1' })

    const line = record(sink.warn.mock.calls[0])
    for (const field of contract.common_fields) expect(line).toHaveProperty(field)
    expect(line).toMatchObject({
      event: 'instance_lease_unavailable',
      level: 'WARNING',
      room: 'r1',
      service: 'collaboration',
    })
    expect(contract.levels).toContain(line.level)
    expect(String(line.ts)).toMatch(new RegExp(contract.ts_pattern))
    expect(line).not.toHaveProperty('component')
    expect(line).not.toHaveProperty('timestamp')
  })

  it('stay below the threshold out of the log', () => {
    const sink = sinkFor()
    const logger = createJsonLogger(sink, 'WARNING')
    logger.debug('formatting_cleanup_accepted')
    logger.info('instance_lease_acquired')
    logger.warn('client_update_rejected')
    logger.error('authoritative_state_mismatch')

    expect(sink.debug).not.toHaveBeenCalled()
    expect(sink.info).not.toHaveBeenCalled()
    expect(record(sink.warn.mock.calls[0]).level).toBe('WARNING')
    expect(record(sink.error.mock.calls[0]).level).toBe('ERROR')
  })

  it('show debug records only when the threshold asks for them', () => {
    const shown = sinkFor()
    createJsonLogger(shown, 'DEBUG').debug('formatting_cleanup_accepted')
    const hidden = sinkFor()
    createJsonLogger(hidden).debug('formatting_cleanup_accepted')

    expect(record(shown.debug.mock.calls[0]).level).toBe('DEBUG')
    expect(hidden.debug).not.toHaveBeenCalled()
  })

  it('keep sensitive field names redacted', () => {
    const sink = sinkFor()
    createJsonLogger(sink).warn('client_update_rejected', { content: MARKER, update_bytes: 9 })

    expect(String(sink.warn.mock.calls[0]?.[0])).not.toContain(MARKER)
  })
})

describe('the log threshold setting', () => {
  it.each([
    [undefined, 'INFO'],
    ['', 'INFO'],
    ['debug', 'DEBUG'],
    ['Warning', 'WARNING'],
    ['CRITICAL', 'CRITICAL'],
  ])('reads %s as %s', (raw, expected) => {
    expect(logLevelFromEnv({ INQTRIX_LOG_LEVEL: raw })).toBe(expected)
  })

  it('names the variable, not the value, when the value is unknown', () => {
    let error: unknown
    try {
      logLevelFromEnv({ INQTRIX_LOG_LEVEL: MARKER })
    } catch (caught) {
      error = caught
    }

    expect(error).toBeInstanceOf(ConfigError)
    expect(String((error as Error).message)).toContain('INQTRIX_LOG_LEVEL')
    expect(String((error as Error).message)).not.toContain(MARKER)
  })
})

describe('a failed start', () => {
  it('names the setting of a configuration error', () => {
    expect(startFailureFields(new ConfigError('INQTRIX_LOG_LEVEL must be one of DEBUG, INFO'))).toEqual({
      reason: 'INQTRIX_LOG_LEVEL must be one of DEBUG, INFO',
    })
  })

  it('names any other error only by its type', () => {
    const fields = startFailureFields(new TypeError(`Invalid URL ${MARKER}`))

    expect(fields).toEqual({ error_type: 'TypeError' })
  })

  it('names the setting of the verification fault gate', () => {
    let error: unknown
    try {
      verificationFaultGateFromEnv({ [VERIFICATION_FAULT_ENV]: MARKER })
    } catch (caught) {
      error = caught
    }

    expect(startFailureFields(error)).toEqual({ reason: `${VERIFICATION_FAULT_ENV} must be 0 or 1` })
  })

  it('names a port that is taken by the code of the system error', async () => {
    const occupant = await listening()
    const sidecar = new CollaborationSidecar(settings({ port: portOf(occupant) }), {
      api: new FakeCollaborationApi(),
      logger: silentLogger,
    })

    const error = await sidecar.start().then(() => undefined, (caught: unknown) => caught)
    await sidecar.stop()
    await closed(occupant)

    expect(startFailureFields(error)).toEqual({ error_type: 'Error', error_code: 'EADDRINUSE' })
  })

  it('names a refused connection by the code of the cause behind fetch', async () => {
    const released = await listening()
    const port = portOf(released)
    await closed(released)

    const error = await fetch(`http://127.0.0.1:${port}/`).then(() => undefined, (caught: unknown) => caught)

    expect(startFailureFields(error)).toEqual({
      error_type: 'TypeError',
      cause_type: 'Error',
      cause_code: 'ECONNREFUSED',
    })
  })

  it('never says the text of an error or of its cause', () => {
    const coded = new TypeError(`fetch failed ${MARKER}`, {
      cause: Object.assign(new Error(`connect ${MARKER}`), { code: 'ECONNREFUSED' }),
    })
    const textual = new Error(`outer ${MARKER}`, { cause: `inner ${MARKER}` })

    expect(startFailureFields(coded)).toEqual({
      error_type: 'TypeError',
      cause_type: 'Error',
      cause_code: 'ECONNREFUSED',
    })
    expect(startFailureFields(textual)).toEqual({ error_type: 'Error', cause_type: 'string' })
  })

  it('leaves out a code that is no stable token', () => {
    const quoting = Object.assign(new Error('failed'), { code: `not a code ${MARKER}` })

    expect(startFailureFields(quoting)).toEqual({ error_type: 'Error' })
  })
})

async function listening(): Promise<Server> {
  const server = createServer()
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve))
  return server
}

function portOf(server: Server): number {
  return (server.address() as AddressInfo).port
}

async function closed(server: Server): Promise<void> {
  await new Promise((resolve) => server.close(resolve))
}

describe('rejected hooks', () => {
  it('reach hocuspocus with code and reason and without a message to print', async () => {
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => undefined)
    const sink = sinkFor()
    const hocuspocus = new Hocuspocus({
      quiet: true,
      onAuthenticate: quietHook('onAuthenticate', async () => {
        throw new CollaborationError('access_revoked', {
          closeCode: 4403,
          closeReason: 'access_revoked:not_found',
        })
      }, createJsonLogger(sink)),
    })

    const rejection = await hocuspocus.hooks('onAuthenticate', {} as never).catch((error: unknown) => error)

    expect(rejection).toMatchObject({ code: 4403, reason: 'access_revoked' })
    expect(consoleError).not.toHaveBeenCalled()
    // The sidecar's own callers get the refusal back; a print of the
    // rejection shows none of it.
    expect(collaborationError(rejection)).toMatchObject({
      closeReason: 'access_revoked:not_found',
      reason: 'access_revoked',
    })
    expect(inspect(rejection, { depth: 8 })).not.toContain('access_revoked:not_found')
    expect(record(sink.info.mock.calls[0])).toMatchObject({
      close_code: 4403,
      close_reason: 'access_revoked:not_found',
      event: 'hook_refused',
      hook: 'onAuthenticate',
      reason: 'access_revoked',
    })
  })

  it('leave the stateless hook as it is, whose empty rejection Hocuspocus drops', () => {
    const onStateless = async (): Promise<void> => undefined
    const onChange = async (): Promise<void> => undefined
    const wrapped = quietHooks({ onChange, onStateless }, createJsonLogger(sinkFor()))

    expect(wrapped.onStateless).toBe(onStateless)
    expect(wrapped.onChange).not.toBe(onChange)
  })

  it('leave other errors as they are', async () => {
    const unexpected = new RangeError('unexpected')
    const sink = sinkFor()
    const hook = quietHook('onChange', async () => {
      throw unexpected
    }, createJsonLogger(sink))

    await expect(hook()).rejects.toBe(unexpected)
    expect(sink.info).not.toHaveBeenCalled()
  })
})

describe('editor schema warnings', () => {
  it('reach the sidecar log as structured records, not as console text', () => {
    const consoleWarn = vi.spyOn(console, 'warn').mockImplementation(() => undefined)
    const sink = sinkFor()
    routeEditorSchemaWarnings(createJsonLogger(sink))

    const projection = serializeEditorJson({
      type: 'doc',
      content: [{ type: 'paragraph', content: [{ type: 'text', text: 'Satz [K1] ﷐' }] }],
    })

    expect(projection).toBe('Satz \\[K1\\] ﷐')
    expect(consoleWarn).not.toHaveBeenCalled()
    expect(sink.warn).toHaveBeenCalledTimes(1)
    expect(record(sink.warn.mock.calls[0])).toMatchObject({
      event: 'citation_labels_left_escaped',
      level: 'WARNING',
      reason: 'placeholder_character_in_document',
      service: 'collaboration',
    })
  })
})
