import { useCallback, useEffect, useRef, useState } from 'react'
import {
  listKnowledgeCollections,
  type ClientOptions,
} from '@/api/inqtrixClient'
import type { VectorIndexRecord } from '@/features/project/types'
import type {
  KnowledgeCollectionInfo,
} from '@/features/researchRuns/types'
import type { KnowledgeCollectionOption } from './types'

export type KnowledgeCollectionsApiHandle = {
  collections: KnowledgeCollectionInfo[]
  error: string | null
  loaded: boolean
  loading: boolean
  refresh: () => Promise<void>
}

/** Authoritative visible knowledge collections, including accepted shares. */
export function useKnowledgeCollectionsApi({
  clientOptions,
  enabled,
  refreshToken = 0,
}: {
  clientOptions: ClientOptions | null
  enabled: boolean
  refreshToken?: number
}): KnowledgeCollectionsApiHandle {
  const [collections, setCollections] = useState<KnowledgeCollectionInfo[]>([])
  const [error, setError] = useState<string | null>(null)
  const [loaded, setLoaded] = useState(false)
  const [loading, setLoading] = useState(false)
  const controllerRef = useRef<AbortController | null>(null)
  const generationRef = useRef(0)

  const refresh = useCallback(async () => {
    controllerRef.current?.abort()
    controllerRef.current = null
    const generation = generationRef.current + 1
    generationRef.current = generation
    if (!enabled || !clientOptions) {
      setCollections([])
      setError(null)
      setLoaded(false)
      setLoading(false)
      return
    }

    const controller = new AbortController()
    controllerRef.current = controller
    setLoading(true)
    try {
      const incoming = await listKnowledgeCollections({
        ...clientOptions,
        signal: controller.signal,
      })
      if (controller.signal.aborted || generation !== generationRef.current) return
      setCollections(incoming)
      setError(null)
      setLoaded(true)
    } catch (cause) {
      if (controller.signal.aborted || generation !== generationRef.current) return
      setError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      if (generation === generationRef.current) {
        setLoading(false)
        if (controllerRef.current === controller) controllerRef.current = null
      }
    }
  }, [clientOptions, enabled])

  useEffect(() => {
    void refresh()
    return () => controllerRef.current?.abort()
  }, [refresh, refreshToken])

  return { collections, error, loaded, loading, refresh }
}

/**
 * Project a server collection into the existing selection vocabulary. A local
 * vector index keeps its stable browser id when it represents the same server
 * collection; accepted shares without a local index use their server id.
 */
export function knowledgeCollectionOptions({
  localIndexes,
  serverCollections,
  serverLoaded,
}: {
  localIndexes: readonly Pick<
    VectorIndexRecord,
    'id' | 'serverCollectionId' | 'status' | 'title'
  >[]
  serverCollections: readonly KnowledgeCollectionInfo[]
  serverLoaded: boolean
}): KnowledgeCollectionOption[] {
  const readyLocal = localIndexes.filter(
    (index) => index.status === 'ready' && Boolean(index.serverCollectionId),
  )
  if (!serverLoaded) {
    return readyLocal.map((index) => ({
      collectionId: index.serverCollectionId as string,
      id: index.id,
      title: index.title,
    }))
  }
  const localByCollectionId = new Map(
    readyLocal.map((index) => [index.serverCollectionId as string, index]),
  )
  return serverCollections.map((collection) => {
    const local = localByCollectionId.get(collection.id)
    return {
      collectionId: collection.id,
      id: local?.id ?? collection.id,
      title: collection.name,
    }
  })
}
