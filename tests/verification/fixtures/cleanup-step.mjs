/**
 * Run a cleanup step that must not mask the result of the run.
 *
 * Its failure stays visible in the run log: reduced to the error class and any
 * HTTP status, because library errors can carry request headers.
 */
export async function attemptCleanup(
  label,
  step,
  write = (line) => process.stderr.write(line),
) {
  try {
    await step()
    return true
  } catch (error) {
    write(`[cleanup] ${label} failed: ${describeFailure(error)}\n`)
    return false
  }
}

/** Removing a file that is already gone is the wanted end state. */
export function ignoreMissing(error) {
  if (error?.code !== 'ENOENT') throw error
}

function describeFailure(error) {
  const status = /\bHTTP (\d{3})\b/.exec(String(error?.message ?? ''))
  const name = typeof error?.name === 'string' ? error.name : 'Error'
  return status ? `${name}, HTTP ${status[1]}` : name
}
