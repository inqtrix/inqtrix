import type { KnowledgeThreadItemRecord } from '@/features/project/types'

type KnowledgeItemStatus = KnowledgeThreadItemRecord['status']

export type KnowledgeItemStatusSnapshot = ReadonlyMap<string, KnowledgeItemStatus>

export function knowledgeItemStatusSnapshot(
  items: readonly KnowledgeThreadItemRecord[],
): Map<string, KnowledgeItemStatus> {
  return new Map(items.map((item) => [item.id, item.status]))
}

export function knowledgeCompletionHandoffId({
  items,
  previousStatuses,
}: {
  items: readonly KnowledgeThreadItemRecord[]
  previousStatuses: KnowledgeItemStatusSnapshot
}): string | null {
  let completedItemId: string | null = null
  for (const item of items) {
    if (
      previousStatuses.get(item.id) === 'running'
      && item.status === 'completed'
      && item.answer
    ) {
      completedItemId = item.id
    }
  }
  return completedItemId
}
