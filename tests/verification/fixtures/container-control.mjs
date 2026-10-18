import { spawn } from 'node:child_process'

const DEFAULT_TIMEOUT_MS = 10_000
const MAX_CAPTURE_BYTES = 1_000_000

export async function requireContainerControlCommand(
  command,
  args,
  cwd,
  operation,
  input = null,
  timeoutMs = DEFAULT_TIMEOUT_MS,
) {
  const result = await runContainerControlCommand(
    command,
    args,
    cwd,
    input,
    timeoutMs,
  )
  if (result.exitCode !== 0) throw new Error(`${operation} failed.`)
  return result
}

export function runContainerControlCommand(
  command,
  args,
  cwd,
  input = null,
  timeoutMs = DEFAULT_TIMEOUT_MS,
) {
  return new Promise((resolvePromise, reject) => {
    const child = spawn(command, args, {
      cwd,
      env: process.env,
      shell: false,
      stdio: [input === null ? 'ignore' : 'pipe', 'pipe', 'pipe'],
    })
    let stdout = ''
    let stderr = ''
    const timeout = setTimeout(() => child.kill('SIGKILL'), timeoutMs)
    child.stdout.setEncoding('utf8')
    child.stderr.setEncoding('utf8')
    child.stdout.on('data', (chunk) => {
      stdout = `${stdout}${chunk}`.slice(0, MAX_CAPTURE_BYTES)
    })
    child.stderr.on('data', (chunk) => {
      stderr = `${stderr}${chunk}`.slice(0, MAX_CAPTURE_BYTES)
    })
    child.once('error', reject)
    child.once('close', (exitCode, signal) => {
      clearTimeout(timeout)
      resolvePromise({ exitCode, signal, stderr, stdout })
    })
    if (input !== null) child.stdin.end(input)
  })
}
