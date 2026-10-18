export const LOAD_SMOKE_CONNECTIONS: 20
export const LOAD_SMOKE_IDENTITIES: 4
export const LOAD_SMOKE_SESSIONS_PER_IDENTITY: 5
export const LOAD_SMOKE_WRITERS: 5
export const LOAD_SOAK_CONNECTIONS: 25
export const LOAD_SOAK_WRITERS: 5
export const LOAD_SOAK_COMMENTERS: 5
export const LOAD_SOAK_READERS: 10
export const LOAD_SOAK_FEATURE_ACTORS: 5

export type LoadDocumentSeed = {
  characterCount: number
  markdown: string
  paragraphCount: number
  profile: 'standard' | 'large-state'
}

export function buildLoadDocumentSeed(options: {
  loadProfile: 'load-smoke' | 'load-soak'
  requestedProfile?: string | null
  runId: string
}): LoadDocumentSeed

export type LoadSmokeSession = {
  access: 'edit' | 'suggest' | 'view'
  expires_at: number
  initial_write_mode: 'edit' | 'suggest' | 'view'
  lease_token: string
  protocol_version: number
  refresh_after: number
  room: string
  schema_version: number
  user: { id: string }
  websocket_path: string
}

export type LoadSmokeFixture = {
  api_probe: {
    contract: 'inqtrix-health-v1'
    url: '/health'
  }
  base_url: string
  sessions: Array<LoadSmokeSession & { reissue_id: string }>
  version: 2
}

export type LoadSoakFixture = LoadSmokeFixture & {
  network_control: {
    authorization_env: string
    contract: 'inqtrix-load-network-control-v1'
    run_id: string
    url: string
  }
  session_reissue: {
    authorization_env: string
    contract: 'inqtrix-collaboration-session-reissue-v1'
    lease_ttl_seconds: 60
    run_id: string
    url: string
  }
}

export function normalizeLoadSmokeBaseURL(value: string): string

export function buildLoadSmokeFixture(options: {
  baseURL: string
  runId: string
  sessions: LoadSmokeSession[]
}): LoadSmokeFixture

export function buildLoadSoakFixture(options: {
  baseURL: string
  controls: {
    authorizationEnv: string
    baseURL: string
    networkPath: string
    reissuePath: string
  }
  runId: string
  sessions: LoadSmokeSession[]
}): LoadSoakFixture

export function writePrivateLoadSmokeFixture(
  path: string,
  fixture: LoadSmokeFixture | LoadSoakFixture,
): Promise<void>
