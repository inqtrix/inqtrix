import type { DragEvent } from 'react'

/** Internal drag type for moving a file row between collections/groups. Kept
 * distinct from external file drops (which carry the native `Files` type and
 * are handled by Dropzone) so the two drag sources never collide. */
export const FILE_DRAG_TYPE = 'application/x-inqtrix-file-id'

export function isInternalFileDrag(event: DragEvent): boolean {
  return Array.from(event.dataTransfer?.types ?? []).includes(FILE_DRAG_TYPE)
}

/** Nominal storage quota for the database storage meter (500 MB). */
export const FILE_QUOTA_BYTES = 500 * 1024 * 1024

export type SortMode = 'recent' | 'name' | 'size' | 'pages'

export type ViewMode = 'list' | 'grid'

/** Active workspace selection. Server collections are distinct from local
 * file-library folders and local pre-build index setup records. */
export type ActiveTarget =
  | { kind: 'all' }
  | { kind: 'collection'; sectionId: string }
  | { kind: 'index'; indexId: string }
  /** `fromIndexId` marks a collection opened deliberately from the index that
   * it backs (the index's server-side storage view). Without it, a collection
   * that BECOMES an index's storage while open is followed back to the index,
   * since it is then no longer a thing of its own. */
  | { kind: 'server-collection'; collectionId: string; fromIndexId?: string }
