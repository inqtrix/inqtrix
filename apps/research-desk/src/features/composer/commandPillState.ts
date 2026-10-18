import { createContext } from 'react'

import type { CommandPillKind } from './CommandPill'

/**
 * What the composer knows about its capsules that a capsule cannot know
 * itself: whether the selected engine refuses its kind, and whether the
 * document it names is still there. One context for both, so a refused and a
 * vanished capsule are marked the same way — warning tone, described, never
 * silently dropped from the request.
 */
export type CommandPillState = {
  /** Kinds the selected engine refuses (reports and canvas documents feed
   * the kernel only). */
  refusedKinds: readonly CommandPillKind[]
  /** The id of the notice that says why: described by it, not repeated. */
  refusalNoticeId?: string
  /** `commandPillKey` of each capsule whose document is no longer available. */
  unavailable: readonly string[]
}

export const NO_COMMAND_PILL_STATE: CommandPillState = { refusedKinds: [], unavailable: [] }

export const CommandPillStateContext = createContext<CommandPillState>(NO_COMMAND_PILL_STATE)

/** THE key of a capsule: its kind and its id within the kind. */
export function commandPillKey(kind: string, id: string): string {
  return `${kind}:${id}`
}
