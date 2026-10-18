import type {
  AgentFeedbackWire,
  AgentMemoryCandidateWire,
  AgentMemoryStatus,
  AgentMemoryWire,
} from '@/api/inqtrixClient'

/** The resolved memory view a refresh produces (the hook layers `loading`
 *  on top). Kept as a pure shape so the merge / 404-fallback / error-map
 *  branches are unit-testable without a React renderer. */
export type ResolvedAgentMemoryState = {
  candidates: AgentMemoryCandidateWire[]
  error: string | null
  feedback: AgentFeedbackWire[]
  memories: AgentMemoryWire[]
  searchQuery: string
  status: AgentMemoryStatus | null
}

/** Synthesized status when the memory surface answers 404: memory is off
 *  for this deployment or the principal is ineligible. The panel renders a
 *  clean "unavailable" state rather than an error — this is an expected
 *  empty state, not a failure. */
export const UNAVAILABLE_AGENT_MEMORY_STATUS: AgentMemoryStatus = {
  available: false,
  durable: false,
  effective_mode: 'off',
  mode: 'off',
  principal_eligible: false,
  provider: 'none',
}

/** Merge the three list responses of a successful refresh into one view.
 *  Status precedence mirrors the backend: the memory list carries the
 *  authoritative status, candidates are the fallback. */
export function mergeAgentMemoryState(
  result: {
    candidates: {
      data: AgentMemoryCandidateWire[]
      status?: AgentMemoryStatus | null
    }
    feedback: { data: AgentFeedbackWire[] }
    memories: { data: AgentMemoryWire[]; status?: AgentMemoryStatus | null }
  },
  searchQuery: string,
): ResolvedAgentMemoryState {
  return {
    candidates: result.candidates.data,
    error: null,
    feedback: result.feedback.data,
    memories: result.memories.data,
    searchQuery,
    status: result.memories.status ?? result.candidates.status ?? null,
  }
}

/** The 404 fallback view: no memories/candidates, an explicit unavailable
 *  status, but any feedback history we could still read is preserved. */
export function unavailableAgentMemoryState(
  feedback: AgentFeedbackWire[],
  searchQuery: string,
): ResolvedAgentMemoryState {
  return {
    candidates: [],
    error: null,
    feedback,
    memories: [],
    searchQuery,
    status: UNAVAILABLE_AGENT_MEMORY_STATUS,
  }
}

/** Map an unknown thrown value to a user-facing message. */
export function agentMemoryErrorMessage(error: unknown): string {
  return error instanceof Error ? error.message : 'Memory unavailable'
}

export function agentMemoryModeLabel(status: AgentMemoryStatus | null): string {
  if (!status) return 'none · off'
  const effective = status.effective_mode || status.mode
  const mode = effective !== status.mode ? `${status.mode} -> ${effective}` : status.mode
  return `${status.provider || 'none'} · ${mode}`
}

export function pendingAgentMemoryCandidates(
  candidates: AgentMemoryCandidateWire[],
): AgentMemoryCandidateWire[] {
  return candidates.filter((candidate) => candidate.status === 'pending')
}

export function visibleAgentFeedback(
  feedback: AgentFeedbackWire[],
  limit = 8,
): AgentFeedbackWire[] {
  return feedback.slice(0, Math.max(0, limit))
}
