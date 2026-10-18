import {
  deleteVectorIndex,
  type ClientOptions,
  type ServerDeletionOperation,
} from '@/api/inqtrixClient'

type DeleteDependencies = {
  deleteIndex: typeof deleteVectorIndex
}

const defaultDependencies: DeleteDependencies = {
  deleteIndex: deleteVectorIndex,
}

export type VectorIndexDeletionRoute =
  | 'knowledge_collection'
  | 'local'
  | 'vector_index'

/**
 * Select the authority that actually owns the index state.
 *
 * A local-first project has no server vector-index row. It may still have a
 * server Knowledge collection after indexing, which must be deleted through
 * the Knowledge lifecycle rather than through a non-existent project record.
 */
export function resolveVectorIndexDeletionRoute({
  knowledgeAvailable,
  projectPersistenceActive,
  serverCollectionId,
}: {
  knowledgeAvailable: boolean
  projectPersistenceActive: boolean
  serverCollectionId?: string | null
}): VectorIndexDeletionRoute {
  if (projectPersistenceActive) return 'vector_index'
  if (knowledgeAvailable && serverCollectionId) return 'knowledge_collection'
  return 'local'
}

/**
 * Start the one server-owned aggregate operation. The durable worker owns
 * cancellation, collection/vector cleanup, residual verification and retry;
 * the browser never performs a second destructive mini-saga.
 */
export async function deleteVectorIndexAggregate({
  collectionId,
  dependencies = defaultDependencies,
  indexId,
  options,
}: {
  collectionId?: string | null
  dependencies?: DeleteDependencies
  indexId: string
  options: ClientOptions
}): Promise<ServerDeletionOperation> {
  return dependencies.deleteIndex(indexId, options, collectionId)
}
