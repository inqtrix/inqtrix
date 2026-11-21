const DEFAULT_FIXTURE_PORT = 5187
const PORT_SETTING = 'INQTRIX_UI_FIXTURE_PORT'

export type FixtureWebServer = {
  baseURL: string
  command: string
  cwd: string
  reuseExistingServer: boolean
  timeout: number
  url: string
}

/**
 * The dev server of the browser fixtures. Each run starts its own server and
 * never takes over one that already listens: that server may belong to another
 * working copy (the main checkout or a worktree), and the run would then report
 * the other copy's code as its own. A taken port therefore fails the run.
 * `INQTRIX_UI_FIXTURE_PORT` gives a working copy its own port, so two copies
 * can run their fixtures at the same time.
 */
export function fixtureWebServer(
  environment: Record<string, string | undefined>,
  appDirectory: string,
): FixtureWebServer {
  const port = fixturePort(environment[PORT_SETTING])
  const baseURL = `http://127.0.0.1:${port}`
  return {
    baseURL,
    command: `npm exec -- vite --host 127.0.0.1 --port ${port} --strictPort`,
    cwd: appDirectory,
    reuseExistingServer: false,
    timeout: 120_000,
    url: `${baseURL}/browser-tests/fixtures/editor-lifecycle.html`,
  }
}

function fixturePort(setting: string | undefined): number {
  const raw = setting?.trim()
  if (!raw) return DEFAULT_FIXTURE_PORT
  const port = Number(raw)
  if (!Number.isInteger(port) || port < 1024 || port > 65_535) {
    throw new Error(`${PORT_SETTING} must be a port from 1024 to 65535, not "${raw}".`)
  }
  return port
}
