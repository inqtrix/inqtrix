import type { JSONContent } from '@tiptap/core'
import { useMemo, type Dispatch, type SetStateAction } from 'react'

import type { AgentResponseForm } from './AgentComposer'
import type { AgentEngineMode } from './agentStatusOverview'
import type { AgentCanvasCommentDraft } from './canvas/commentQueue'
import {
  DEFAULT_AGENT_SOURCE_POLICY,
  type AgentKnowledgeSwitch,
  type AgentSourcePolicy,
} from './executionPolicy'

/**
 * Everything the Agent Desk draft carries besides its plain text: the
 * composer document with its capsules where the user put them, what is
 * attached to it, and the run setup chosen for it.
 *
 * It lives in the desk, not in the workspace: the desk unmounts a workspace
 * on every view switch, and a draft that lived in the workspace came back as
 * bare text — capsules gone from the sentence, attachments dropped, the
 * engine reset. The plain text lives beside it (`agentDraftQuestion`), which
 * the menus and the empty-state examples read and write.
 */
export type AgentComposerDraft = {
  /** Selection comments queued for the next submission. */
  canvasCommentQueue: AgentCanvasCommentDraft[]
  /** The composer document, capsules included; null before the first edit. */
  doc: JSONContent | null
  engineMode: AgentEngineMode | null
  /** The knowledge switch of a session that does not exist yet. */
  knowledgeSwitch: AgentKnowledgeSwitch | undefined
  pinnedCanvasArtifactId: string | null
  reportIds: string[]
  responseForm: AgentResponseForm
  ruleIds: string[]
  skillIds: string[]
  /** The source policy of a session that does not exist yet. */
  sourcePolicy: AgentSourcePolicy
}

export function emptyAgentComposerDraft(): AgentComposerDraft {
  return {
    canvasCommentQueue: [],
    doc: null,
    engineMode: null,
    knowledgeSwitch: undefined,
    pinnedCanvasArtifactId: null,
    reportIds: [],
    responseForm: 'auto',
    ruleIds: [],
    skillIds: [],
    sourcePolicy: { ...DEFAULT_AGENT_SOURCE_POLICY },
  }
}

/** A setter for ONE field of the draft with the `useState` contract: a
 * value, or an updater that receives the field's current value. */
export function draftFieldSetter<K extends keyof AgentComposerDraft>(
  setDraft: Dispatch<SetStateAction<AgentComposerDraft>>,
  key: K,
): Dispatch<SetStateAction<AgentComposerDraft[K]>> {
  return (next) => setDraft((draft) => {
    const value = typeof next === 'function'
      ? (next as (current: AgentComposerDraft[K]) => AgentComposerDraft[K])(draft[key])
      : next
    return Object.is(value, draft[key]) ? draft : { ...draft, [key]: value }
  })
}

/** One field of the draft as `[value, setValue]`; the setter keeps its
 * identity, so effects and callbacks that depend on it do not rerun. */
export function useDraftField<K extends keyof AgentComposerDraft>(
  draft: AgentComposerDraft,
  setDraft: Dispatch<SetStateAction<AgentComposerDraft>>,
  key: K,
): [AgentComposerDraft[K], Dispatch<SetStateAction<AgentComposerDraft[K]>>] {
  const setField = useMemo(() => draftFieldSetter(setDraft, key), [setDraft, key])
  return [draft[key], setField]
}
