/**
 * The browser keys the retired mission engine's plan gate left behind.
 *
 * Each unsent plan decision was kept in `localStorage` under this prefix
 * until the gate was decided. Nothing reads or writes those keys any more,
 * so a decision that was never sent left its draft in the profile — the
 * unsent text of one account where the next one signs in. The app sweeps
 * them when it starts, before anything renders and whatever the sign-in
 * mode; after the first start there is nothing left to find.
 */
const RETIRED_PLAN_DRAFT_PREFIX = 'inqtrix.agent.plan-draft:'

/** The part of a storage area the sweep uses. */
type SweptStorage = Pick<Storage, 'key' | 'length' | 'removeItem'>

function localStorageArea(): SweptStorage | undefined {
  try {
    return globalThis.localStorage
  } catch {
    // Blocked site data throws on the property itself.
    return undefined
  }
}

export function sweepRetiredPlanDrafts(
  storage: SweptStorage | undefined = localStorageArea(),
): void {
  if (!storage) return
  try {
    const keys: string[] = []
    for (let index = 0; index < storage.length; index += 1) {
      const key = storage.key(index)
      if (key?.startsWith(RETIRED_PLAN_DRAFT_PREFIX)) keys.push(key)
    }
    for (const key of keys) storage.removeItem(key)
  } catch {
    // A storage area that refuses to be read holds nothing the app can
    // sweep; the start goes on.
  }
}
