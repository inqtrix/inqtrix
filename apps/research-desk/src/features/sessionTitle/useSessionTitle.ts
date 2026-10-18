import { useEffect, useRef, type Dispatch } from 'react'

import { generateSessionTitle, type SessionTitleResponse } from '@/api/inqtrixClient'
import type { ProjectState } from '@/features/project/types'
import type { ResearchDeskAction } from '@/features/researchDesk/state'
import type { TitleKind, TitledRecord } from './model'
import { readTitleDraft, writeTitleDraft } from './draftStorage'

/** Lives above both workspaces: changing desks never cancels title work. */
export function useSessionTitle({
  state, dispatch, enabled, demo, chatPersisted, workspaceId, apiKey, userId, language, stack,
}: {
  state: ProjectState
  dispatch: Dispatch<ResearchDeskAction>
  enabled: boolean
  demo: boolean
  chatPersisted: boolean
  workspaceId: string
  apiKey?: string
  userId?: string | null
  language: string
  stack?: string
}) {
  const pending = useRef(new Map<string, AbortController>())
  const completed = useRef(new Set<string>())
  const observed = useRef(new Set<string>())
  const identity = `${state.projectEpoch}:${workspaceId}:${userId ?? ''}:${apiKey ?? ''}:${demo}`
  const currentIdentity = useRef(identity)
  currentIdentity.current = identity

  useEffect(() => {
    const requests = pending.current
    const finished = completed.current
    const known = observed.current
    return () => {
      for (const controller of requests.values()) controller.abort()
      requests.clear()
      for (const id of known) finished.add(id)
    }
  }, [identity])

  useEffect(() => {
    const candidates: (readonly [TitleKind, string, TitledRecord & { deletion?: unknown; persistable?: boolean }])[] = [
      ...Object.entries(state.chatThreads).map(([id, row]) => ['chat', id, row] as const),
      ...Object.entries(state.agentSessions).map(([id, row]) => ['agent', id, row] as const),
    ]
    const liveIds = new Set(candidates.flatMap(([, , row]) => row.titleRequest && row.titleSource === 'initial'
      && !row.deletion ? [row.titleRequest.id] : []))
    for (const id of liveIds) observed.current.add(id)
    for (const [id, controller] of pending.current) if (!liveIds.has(id)) controller.abort()
    for (const id of completed.current) if (!liveIds.has(id) && !pending.current.has(id)) {
      completed.current.delete(id)
      observed.current.delete(id)
    }

    for (const [kind, id, row] of candidates) {
      const persisted = !demo && (kind === 'agent' ? row.persistable !== false : chatPersisted)
      const draftScope = `${userId ?? 'operator'}:${workspaceId}`
      if (persisted && (row.titlePending && row.titleSource === 'manual' || row.titleConflict)) {
        writeTitleDraft(draftScope, kind, id, { title: row.titleConflict ?? row.title, revision: row.titleRevision ?? 0 })
      } else if (persisted && row.titleServerReady) {
        const draft = readTitleDraft(draftScope, kind, id)
        if (draft) {
          if (row.titleSource === 'manual' && row.title === draft.title) writeTitleDraft(draftScope, kind, id, null)
          else dispatch({ type: 'restoreSessionTitleDraft', kind, id, title: draft.title, projectEpoch: state.projectEpoch })
        }
      }
      const request = row.titleRequest
      if (!request || !liveIds.has(request.id) || pending.current.has(request.id) || completed.current.has(request.id)) continue
      if (persisted && (!row.titleServerReady || row.titlePending)) continue
      const controller = new AbortController()
      pending.current.set(request.id, controller)
      const projectEpoch = state.projectEpoch

      const finish = (result: SessionTitleResponse) => {
        if (currentIdentity.current !== identity) return
        completed.current.add(request.id)
        dispatch({ type: 'finishSessionTitle', kind, id, requestId: request.id, projectEpoch,
          title: result.title, reason: result.reason, current: result.current })
      }

      const work = async () => {
        if (demo) {
          await new Promise<void>((resolve) => {
            const timer = window.setTimeout(resolve, 500)
            controller.signal.addEventListener('abort', () => { window.clearTimeout(timer); resolve() }, { once: true })
          })
          return { title: request.question.trim().split(/\s+/).slice(0, 6).join(' ').slice(0, 60).replace(/[.:]+$/, '') }
        }
        if (!enabled) return { title: null, reason: 'disabled' }
        return generateSessionTitle({ question: request.question, language: request.language ?? language,
          stack: request.stack === undefined ? stack : request.stack ?? undefined,
          ...(persisted ? { target: { kind, id, revision: row.titleRevision ?? 0 } } : {}) },
        { apiKey, workspaceId, signal: controller.signal })
      }

      void work().then((result) => {
        if (!controller.signal.aborted) finish(result)
      }).catch(() => {
        if (!controller.signal.aborted) finish({ title: null, reason: 'unavailable' })
      }).finally(() => {
        pending.current.delete(request.id)
      })
    }
  }, [apiKey, chatPersisted, demo, dispatch, enabled, identity, language, stack, state.agentSessions, state.chatThreads, state.projectEpoch, userId, workspaceId])
}
