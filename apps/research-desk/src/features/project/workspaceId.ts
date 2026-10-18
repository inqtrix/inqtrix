const WORKSPACE_ID_STORAGE_KEY = 'inqtrix.researchDesk.workspaceId'
const WORKSPACE_ID_PATTERN = /^ws_[A-Za-z0-9_-]{8,77}$/

export function createWorkspaceId() {
  const randomId = globalThis.crypto?.randomUUID?.()
  if (randomId) return `ws_${randomId.replace(/-/g, '')}`

  const bytes = new Uint8Array(16)
  if (globalThis.crypto?.getRandomValues) {
    globalThis.crypto.getRandomValues(bytes)
    return `ws_${Array.from(bytes, byteToHex).join('')}`
  }

  const timePart = Date.now().toString(36)
  const randomPart = Math.random().toString(36).slice(2, 18)
  return `ws_${timePart}_${randomPart}`
}

export function getOrCreateBrowserWorkspaceId() {
  const stored = readBrowserWorkspaceId()
  if (stored) return stored

  const workspaceId = createWorkspaceId()
  rememberBrowserWorkspaceId(workspaceId)
  return workspaceId
}

export function isWorkspaceId(value: unknown): value is string {
  return typeof value === 'string' && WORKSPACE_ID_PATTERN.test(value)
}

export function rememberBrowserWorkspaceId(workspaceId: string) {
  if (!isWorkspaceId(workspaceId) || !canUseLocalStorage()) return
  try {
    window.localStorage.setItem(WORKSPACE_ID_STORAGE_KEY, workspaceId)
  } catch {
    // Browsers may deny storage in private contexts; the in-memory project id still works.
  }
}

function readBrowserWorkspaceId() {
  if (!canUseLocalStorage()) return null
  try {
    const value = window.localStorage.getItem(WORKSPACE_ID_STORAGE_KEY)
    return isWorkspaceId(value) ? value : null
  } catch {
    return null
  }
}

function canUseLocalStorage() {
  if (typeof window === 'undefined') return false
  try {
    return typeof window.localStorage !== 'undefined'
  } catch {
    return false
  }
}

function byteToHex(byte: number) {
  return byte.toString(16).padStart(2, '0')
}
