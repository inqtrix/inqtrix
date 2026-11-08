import { randomUUID } from 'node:crypto'

import { logLevelFromEnv, settingsFromEnv, startFailureFields } from './config'
import { consoleSink, createJsonLogger, routeEditorSchemaWarnings, routeLibraryConsole } from './logger'
import { CollaborationSidecar } from './sidecar'
import { verificationFaultGateFromEnv } from './verificationFault'

const sink = consoleSink()

async function main(): Promise<void> {
  const logger = createJsonLogger(sink, logLevelFromEnv(process.env))
  routeEditorSchemaWarnings(logger)
  routeLibraryConsole(logger)
  const settings = settingsFromEnv(process.env, randomUUID())
  const verificationFaults = verificationFaultGateFromEnv(process.env)
  verificationFaults?.reset()
  const sidecar = new CollaborationSidecar(settings, {
    logger,
    verificationFaults,
  })
  let closing: Promise<void> | null = null

  const reloadVerificationFault = (): void => {
    if (!verificationFaults) return
    try {
      verificationFaults.reload()
    } catch {
      // The loopback controller requires a loaded acknowledgement and reports
      // malformed or missing records as a failed verification operation.
    }
  }

  const stop = (signal: NodeJS.Signals): void => {
    if (closing) return
    logger.info('shutdown_requested', { signal })
    closing = sidecar.stop().catch(() => {
      logger.error('shutdown_failed')
      process.exitCode = 1
    }).finally(() => {
      if (verificationFaults) {
        process.removeListener('SIGUSR2', reloadVerificationFault)
        verificationFaults.reset()
      }
    })
  }

  if (verificationFaults) process.on('SIGUSR2', reloadVerificationFault)
  process.once('SIGINT', stop)
  process.once('SIGTERM', stop)
  await sidecar.start()
  if (closing) await closing
}

main().catch((error: unknown) => {
  createJsonLogger(sink).error('collaboration_sidecar_start_failed', startFailureFields(error))
  process.exitCode = 1
})
