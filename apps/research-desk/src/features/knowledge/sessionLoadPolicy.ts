import type { KnowledgeSessionRecord } from '@/features/project/types'

type SessionItemsLoadMergeInput = {
  localItemCount: number
  localSession: KnowledgeSessionRecord | undefined
  serverItemCount: number
  serverSession: KnowledgeSessionRecord
}

type SessionItemsLoadMergeDecision = {
  applyServerState: boolean
  markItemsPayloadLoaded: boolean
  markItemsLoadResolved: boolean
}

type SessionLoadSurfaceInput = {
  selectedSessionId: string | null
  sessionId: string
  surfaceErrors: boolean
}

export function shouldSurfaceKnowledgeSessionItemsLoadResult({
  selectedSessionId,
  sessionId,
  surfaceErrors,
}: SessionLoadSurfaceInput): boolean {
  return surfaceErrors && selectedSessionId === sessionId
}

export function decideKnowledgeSessionItemsLoadMerge({
  localItemCount,
  localSession,
  serverItemCount,
  serverSession,
}: SessionItemsLoadMergeInput): SessionItemsLoadMergeDecision {
  if (!localSession) {
    return {
      applyServerState: false,
      markItemsLoadResolved: false,
      markItemsPayloadLoaded: false,
    }
  }

  if (serverSession.updatedAt >= localSession.updatedAt) {
    return {
      applyServerState: true,
      markItemsLoadResolved: true,
      markItemsPayloadLoaded: true,
    }
  }

  return {
    applyServerState: false,
    markItemsLoadResolved: true,
    markItemsPayloadLoaded: localItemCount > 0 || serverItemCount === 0,
  }
}
