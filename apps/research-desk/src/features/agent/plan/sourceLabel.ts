/**
 * Pure display helpers for a plan task's retrieval SOURCE ("wo").
 *
 * The plan card answers three questions per task before approval:
 * purpose (objective), the literal queries, and WHERE they run — this
 * module renders the "where" from data the payload already carries
 * (`params.collection_ids`, `params.recency`) plus the caller's
 * collection titles. Kept pure so vitest covers the mapping without
 * mounting the review body.
 */

export type PlanSourceInfo = {
  /** The caller-visible collections (id -> display title). */
  collections: { id: string; title: string }[]
  /** Configured vector-backend label (e.g. "Qdrant"); `null` hides the
   * parenthesis and leaves the honest generic "Wissens-Index". */
  vectorBackendLabel: string | null
}

export type PlanSourceLabels = {
  allCollections: string
  knowledgeIndex: string
  recency: string
  web: string
}

/** Display name for a configured vector backend id ("qdrant" -> "Qdrant"). */
export function vectorBackendDisplay(
  backend: string | null | undefined,
): string | null {
  const value = (backend ?? '').trim()
  if (!value) return null
  return value.charAt(0).toUpperCase() + value.slice(1)
}

function collectionTitle(
  info: PlanSourceInfo,
  collectionId: string,
): string {
  const match = info.collections.find(
    (collection) => collection.id === collectionId,
  )
  // Fall back to the raw id — an unknown reference stays VISIBLE (it is
  // exactly what the validator would reject), never silently pretty.
  return match ? `${match.title} (${collectionId})` : collectionId
}

/**
 * One-line source label for a plan task; `null` for kinds without a
 * retrieval source (synthesis).
 */
export function planTaskSourceLabel(
  task: {
    toolKind: string
    params: Record<string, unknown>
  },
  info: PlanSourceInfo,
  labels: PlanSourceLabels,
): string | null {
  if (task.toolKind === 'web_research' || task.toolKind === 'web_instant') {
    const recency =
      typeof task.params.recency === 'string' && task.params.recency
        ? ` · ${labels.recency}: ${task.params.recency}`
        : ''
    return `${labels.web}${recency}`
  }
  if (task.toolKind === 'rag_query' || task.toolKind === 'file_analysis') {
    const index = info.vectorBackendLabel
      ? `${labels.knowledgeIndex} (${info.vectorBackendLabel})`
      : labels.knowledgeIndex
    const ids = Array.isArray(task.params.collection_ids)
      ? task.params.collection_ids.map(String).filter((id) => id.trim())
      : []
    if (ids.length === 0) {
      // No explicit narrowing: the task inherits the run's selected
      // collections — say so instead of showing nothing.
      return `${index} → ${labels.allCollections}`
    }
    const titles = ids.map((id) => collectionTitle(info, id)).join(', ')
    return `${index} → ${titles}`
  }
  return null
}
