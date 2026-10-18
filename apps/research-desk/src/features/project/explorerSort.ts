/**
 * Shared sidebar ordering for every desk rail:
 * one sort vocabulary — `recent` (default: items by last activity,
 * folders alphabetical, pinned rows in pin order), `name` (everything
 * alphabetical), `manual` (the pre-existing behavior: explicit order
 * arrays, free drag placement, pinned rows follow the master order).
 * A drag in an automatic mode adopts the visible order and switches
 * the desk to `manual` — visibly, via the header sort control, never
 * silently. All helpers are pure so each desk's section builder can
 * apply them without owning a copy of the rules.
 */

export type ExplorerSortMode = 'recent' | 'name' | 'manual'

export type ExplorerSortDesk = 'chat' | 'knowledge' | 'editor' | 'agent'

export type ExplorerSortState = Record<ExplorerSortDesk, ExplorerSortMode>

export const EXPLORER_SORT_MODES: readonly ExplorerSortMode[] = [
  'recent',
  'name',
  'manual',
]

export function defaultExplorerSortState(): ExplorerSortState {
  return { agent: 'recent', chat: 'recent', editor: 'recent', knowledge: 'recent' }
}

function isExplorerSortMode(value: unknown): value is ExplorerSortMode {
  return EXPLORER_SORT_MODES.includes(value as ExplorerSortMode)
}

/** Manifest/wire values resolve to a valid state; unknowns fall back to
 * the default mode per desk (schema default, not an error path). */
export function resolveExplorerSortState(value: unknown): ExplorerSortState {
  const record = value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {}
  const fallback = defaultExplorerSortState()
  return {
    agent: isExplorerSortMode(record.agent) ? record.agent : fallback.agent,
    chat: isExplorerSortMode(record.chat) ? record.chat : fallback.chat,
    editor: isExplorerSortMode(record.editor) ? record.editor : fallback.editor,
    knowledge: isExplorerSortMode(record.knowledge)
      ? record.knowledge
      : fallback.knowledge,
  }
}

const collator = new Intl.Collator(undefined, {
  numeric: true,
  sensitivity: 'base',
})

function timeValue(iso: string): number {
  const parsed = Date.parse(iso)
  return Number.isNaN(parsed) ? 0 : parsed
}

/**
 * Items of one section. `recent`: newest activity first (title, then
 * original position break ties so the order is deterministic). `name`:
 * alphabetical, locale-aware with numeric collation ("Bericht 2" before
 * "Bericht 10"). `manual`: the given array untouched.
 */
export function sortExplorerItems<T>(
  items: readonly T[],
  mode: ExplorerSortMode,
  timeOf: (item: T) => string,
  titleOf: (item: T) => string,
): T[] {
  if (mode === 'manual') return [...items]
  const indexed = items.map((item, index) => ({ index, item }))
  if (mode === 'name') {
    indexed.sort(
      (a, b) =>
        collator.compare(titleOf(a.item), titleOf(b.item))
        || a.index - b.index,
    )
  } else {
    indexed.sort(
      (a, b) =>
        timeValue(timeOf(b.item)) - timeValue(timeOf(a.item))
        || collator.compare(titleOf(a.item), titleOf(b.item))
        || a.index - b.index,
    )
  }
  return indexed.map((entry) => entry.item)
}

/** Folders are alphabetical in BOTH automatic modes (the industry
 * convention for folder rows) and untouched in `manual`. */
export function sortExplorerFolders<T>(
  folders: readonly T[],
  mode: ExplorerSortMode,
  titleOf: (folder: T) => string,
): T[] {
  if (mode === 'manual') return [...folders]
  const indexed = folders.map((folder, index) => ({ folder, index }))
  indexed.sort(
    (a, b) =>
      collator.compare(titleOf(a.folder), titleOf(b.folder))
      || a.index - b.index,
  )
  return indexed.map((entry) => entry.folder)
}

/**
 * The pinned section. Automatic modes render pin order (the stored pin
 * array appends on pin, so first-pinned stays on top — stable, a new
 * pin never reshuffles existing ones). `manual` keeps the master-list
 * order, where the user's own drag placement decides.
 */
export function orderPinnedExplorerItems<T>(
  items: readonly T[],
  pinnedIds: readonly string[],
  mode: ExplorerSortMode,
  idOf: (item: T) => string,
): T[] {
  if (mode === 'manual') return [...items]
  const position = new Map(pinnedIds.map((id, index) => [id, index]))
  const indexed = items.map((item, index) => ({ index, item }))
  indexed.sort(
    (a, b) =>
      (position.get(idOf(a.item)) ?? Number.MAX_SAFE_INTEGER)
        - (position.get(idOf(b.item)) ?? Number.MAX_SAFE_INTEGER)
      || a.index - b.index,
  )
  return indexed.map((entry) => entry.item)
}

/**
 * The full visible id sequence a drag adopts when it switches an
 * automatic mode to `manual`: the sorted view becomes the new explicit
 * order (so nothing jumps at the moment of the switch), and every id
 * the view did not cover (search-filtered rows, records of other
 * owners) keeps its previous relative position appended behind.
 */
export function adoptVisibleExplorerOrder(
  currentOrder: readonly string[],
  visibleIds: readonly string[],
): string[] {
  const known = new Set(currentOrder)
  const adopted = visibleIds.filter((id) => known.has(id))
  const covered = new Set(adopted)
  return [...adopted, ...currentOrder.filter((id) => !covered.has(id))]
}
