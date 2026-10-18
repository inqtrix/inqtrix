import { useEffect, useState } from 'react'
import type { KnowledgeDataSource } from './types'

export type FileAccessProbe = 'allowed' | 'checking' | 'unavailable'

/** Resolve the Original-file affordance against the current principal. The
 * metadata `file_id` is only a pointer; it is never treated as proof of access. */
export function useFileAccessProbe(
  dataSource: KnowledgeDataSource,
  fileId: string | null,
): FileAccessProbe {
  const [state, setState] = useState<FileAccessProbe>('unavailable')

  useEffect(() => {
    if (!fileId || !dataSource.canLoadFileContent || !dataSource.loadFileContent) {
      setState('unavailable')
      return undefined
    }
    let ignore = false
    setState('checking')
    dataSource.canLoadFileContent(fileId)
      .then((allowed) => {
        if (!ignore) setState(allowed ? 'allowed' : 'unavailable')
      })
      .catch((error) => {
        if (ignore) return
        console.warn('Original-file access probe failed.', error)
        setState('unavailable')
      })
    return () => {
      ignore = true
    }
  }, [dataSource, fileId])

  return state
}
