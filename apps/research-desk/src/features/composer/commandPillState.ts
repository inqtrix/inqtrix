import { createContext } from 'react'

/**
 * What the composer knows about its capsules that a capsule cannot know
 * itself: whether the document it names is still there. A vanished capsule
 * is marked — warning tone, described, never silently dropped from the
 * request.
 */
export type CommandPillState = {
  /** `commandPillKey` of each capsule whose document is no longer available. */
  unavailable: readonly string[]
}

export const NO_COMMAND_PILL_STATE: CommandPillState = { unavailable: [] }

export const CommandPillStateContext = createContext<CommandPillState>(NO_COMMAND_PILL_STATE)

/** THE key of a capsule: its kind and its id within the kind. */
export function commandPillKey(kind: string, id: string): string {
  return `${kind}:${id}`
}
