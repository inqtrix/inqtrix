import { useCallback, useEffect, useMemo, useState } from 'react'

import {
  acceptAgentMemoryCandidate,
  clearAgentMemories,
  deleteAgentMemory,
  hasHttpStatus,
  listAgentMemoryFeedback,
  listAgentMemories,
  listAgentMemoryCandidates,
  rejectAgentMemoryCandidate,
  submitAgentRunFeedback,
  updateAgentMemory,
  type AgentMemoryCandidateWire,
  type AgentFeedbackWire,
  type AgentMemoryStatus,
  type AgentMemoryWire,
  type ClientOptions,
} from '@/api/inqtrixClient'
import {
  agentMemoryErrorMessage,
  mergeAgentMemoryState,
  unavailableAgentMemoryState,
} from './memoryModel'

type AgentMemoryState = {
  candidates: AgentMemoryCandidateWire[]
  error: string | null
  feedback: AgentFeedbackWire[]
  loading: boolean
  memories: AgentMemoryWire[]
  searchQuery: string
  status: AgentMemoryStatus | null
}

export type AgentMemoryHandle = AgentMemoryState & {
  acceptCandidate: (candidateId: string) => Promise<void>
  clearAll: () => Promise<void>
  deleteMemory: (memoryId: string) => Promise<void>
  refresh: () => Promise<void>
  rejectCandidate: (candidateId: string) => Promise<void>
  setSearchQuery: (query: string) => void
  submitFeedback: (
    memory: AgentMemoryWire,
    feedback: 'positive' | 'negative',
  ) => Promise<void>
  updateMemory: (memory: AgentMemoryWire) => Promise<void>
}

export function useAgentMemory(options: ClientOptions): AgentMemoryHandle {
  const [state, setState] = useState<AgentMemoryState>({
    candidates: [],
    error: null,
    feedback: [],
    loading: true,
    memories: [],
    searchQuery: '',
    status: null,
  })
  const stableOptions = useMemo(
    () => ({
      apiKey: options.apiKey,
      baseUrl: options.baseUrl,
      workspaceId: options.workspaceId,
    }),
    [options.apiKey, options.baseUrl, options.workspaceId],
  )
  const searchQuery = state.searchQuery

  const refresh = useCallback(async () => {
    setState((current) => ({ ...current, error: null, loading: true }))
    try {
      const [memories, candidates, feedback] = await Promise.all([
        listAgentMemories(stableOptions, { q: searchQuery }),
        listAgentMemoryCandidates(stableOptions),
        listAgentMemoryFeedback(stableOptions, { limit: 25 }),
      ])
      setState({
        ...mergeAgentMemoryState({ candidates, feedback, memories }, searchQuery),
        loading: false,
      })
    } catch (error) {
      if (hasHttpStatus(error, 404)) {
        let feedback: AgentFeedbackWire[]
        try {
          feedback = (
            await listAgentMemoryFeedback(stableOptions, { limit: 25 })
          ).data
        } catch {
          feedback = []
        }
        setState({
          ...unavailableAgentMemoryState(feedback, searchQuery),
          loading: false,
        })
        return
      }
      setState((current) => ({
        ...current,
        error: agentMemoryErrorMessage(error),
        loading: false,
      }))
    }
  }, [searchQuery, stableOptions])

  useEffect(() => {
    void refresh()
  }, [refresh])

  return {
    ...state,
    acceptCandidate: async (candidateId: string) => {
      await acceptAgentMemoryCandidate(candidateId, {}, stableOptions)
      await refresh()
    },
    clearAll: async () => {
      await clearAgentMemories(stableOptions)
      await refresh()
    },
    deleteMemory: async (memoryId: string) => {
      await deleteAgentMemory(memoryId, stableOptions)
      await refresh()
    },
    refresh,
    rejectCandidate: async (candidateId: string) => {
      await rejectAgentMemoryCandidate(candidateId, stableOptions)
      await refresh()
    },
    setSearchQuery: (query: string) => {
      setState((current) => ({ ...current, searchQuery: query }))
    },
    submitFeedback: async (
      memory: AgentMemoryWire,
      feedback: 'positive' | 'negative',
    ) => {
      // Feedback is scoped to the run that produced the memory; without a
      // source run there is no run to attribute it to, so the caller must
      // gate the control (never post a runless feedback).
      if (!memory.source_run_id) return
      await submitAgentRunFeedback(
        memory.source_run_id,
        { feedback, memory_id: memory.id },
        stableOptions,
      )
      await refresh()
    },
    updateMemory: async (memory: AgentMemoryWire) => {
      await updateAgentMemory(
        memory.id,
        {
          category: memory.category,
          content: memory.content,
          scope: memory.scope,
        },
        stableOptions,
      )
      await refresh()
    },
  }
}
