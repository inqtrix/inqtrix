/** Per-tab recovery of a submitted manual rename whose save is unresolved.
 * Different tabs keep their own competing intent. Recovery never writes it
 * automatically over the server's current title. */
const PREFIX = 'inqtrix-title-draft:'
type Draft = { title: string; revision: number }

function key(scope: string, kind: string, id: string) {
  return `${PREFIX}${JSON.stringify([scope, kind, id])}`
}

export function readTitleDraft(scope: string, kind: string, id: string): Draft | null {
  try {
    const raw = globalThis.sessionStorage?.getItem(key(scope, kind, id))
    if (!raw) return null
    const value = JSON.parse(raw) as Partial<Draft>
    return value && typeof value.title === 'string' && value.title.trim()
      && Number.isSafeInteger(value.revision) && Number(value.revision) >= 0 ? value as Draft : null
  } catch { return null }
}

export function writeTitleDraft(scope: string, kind: string, id: string, value: Draft | null): void {
  try {
    if (value) globalThis.sessionStorage?.setItem(key(scope, kind, id), JSON.stringify(value))
    else globalThis.sessionStorage?.removeItem(key(scope, kind, id))
  } catch { /* Storage may be unavailable; the live rename remains usable. */ }
}

export function clearTitleDrafts(): void {
  try {
    const storage = globalThis.sessionStorage
    for (let index = storage.length - 1; index >= 0; index -= 1) {
      const candidate = storage.key(index)
      if (candidate?.startsWith(PREFIX)) storage.removeItem(candidate)
    }
  } catch { /* A refused storage area must not block an identity change. */ }
}

/** Loading an auth session is not an identity transition. */
export function updateTitleDraftIdentity(previous: string | null | undefined, next: string | null, confirmed: boolean) {
  if (!confirmed) return previous
  if (previous === undefined && next === null) return previous
  if (previous !== undefined && previous !== next) clearTitleDrafts()
  return next
}
