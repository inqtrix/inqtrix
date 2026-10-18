/**
 * The explicit "move this project to the server" import.
 *
 * The MANUAL opt-in path, surfaced now only for the apikey / local-first tiers
 * (the Topbar import button is shown iff ``canPersistProject && !serverSyncEnabled``).
 * For an authenticated cookie-session user (local/oidc) server sync is
 * AUTOMATIC — ResearchDesk derives ``serverSyncEnabled`` from the live session
 * so the project hydrates and autosaves without a separate button; this import is
 * never reached. The two paths share the same machinery; only the trigger
 * differs (session-derived vs. button).
 *
 * Project-level on purpose: the import pushes EVERY persisted entity (chat
 * threads/groups/messages AND editor documents/folders/comments) in one
 * flow and flips ``serverSyncEnabled`` once, rather than each per-entity
 * hook owning a separate import that races on the shared flag/pending state.
 * After the push + opt-in, the per-entity sync hooks hydrate the just-pushed
 * data (server == local → no re-push) and take over autosave.
 *
 * Before the first push, the local graph is detached onto globally unique ids
 * and becomes the active project. That mapping remains stable across retries
 * in the current project epoch, so a partial push converges instead of writing
 * a second graph. Errors propagate to the caller (the ResearchDesk handler
 * surfaces them via the project-action banner — No Silent Fallbacks), while
 * ``importPending`` always clears.
 */

import { useCallback, useRef, useState } from 'react'
import type { Dispatch } from 'react'

import type { ClientOptions } from '@/api/inqtrixClient'
import { pushAllChatEntities } from '@/features/chat/chatHistorySync'
import { pushAllEditorEntities } from '@/features/editor/editorSync'
import { pushAllAssetEntities } from '@/features/fileLibrary/assetSync'
import { pushAllVectorIndexEntities } from '@/features/fileLibrary/vectorIndexSync'
import { detachProjectResourceGraph } from '@/features/project/detachedImport'
import type { ProjectState } from '@/features/project/types'
import type { ResearchDeskAction } from '@/features/researchDesk/state'

type UseProjectServerImportOptions = {
  apiKey: string | undefined
  /** The server offers durable project persistence (capability on, not demo). */
  canPersist: boolean
  dispatch: Dispatch<ResearchDeskAction>
  state: ProjectState
  workspaceId: string
}

export type ProjectServerImportHandle = {
  importPending: boolean
  importToServer: () => Promise<void>
}

export function useProjectServerImport({
  apiKey,
  canPersist,
  dispatch,
  state,
  workspaceId,
}: UseProjectServerImportOptions): ProjectServerImportHandle {
  const [importPending, setImportPending] = useState(false)
  const stateRef = useRef(state)
  stateRef.current = state
  const optionsRef = useRef<ClientOptions>({ apiKey, workspaceId })
  optionsRef.current = { apiKey, workspaceId }
  const preparedRef = useRef<{ projectEpoch: number; workspaceId: string } | null>(null)

  const importToServer = useCallback(async () => {
    if (!canPersist || importPending) return
    setImportPending(true)
    try {
      const current = stateRef.current
      const options = optionsRef.current
      const prepared = preparedRef.current
      const project = prepared?.projectEpoch === current.projectEpoch
        && prepared.workspaceId === workspaceId
        ? current
        : detachProjectResourceGraph(current, workspaceId)
      if (project !== current) {
        preparedRef.current = {
          projectEpoch: current.projectEpoch + 1,
          workspaceId,
        }
        dispatch({ state: project, type: 'hydrateProject' })
      }
      // The four pushes run in sequence and serverSyncEnabled is flipped only
      // AFTER all of them succeed, so a mid-flight rejection never enables sync
      // over partial data. A failed push leaves the entities it already wrote
      // on the server (there is no cross-push transaction), but recovery is
      // simply to re-run the import: every push is an idempotent upsert keyed
      // by id, so a retry converges. The rejection surfaces to the caller (the
      // ResearchDesk project-action banner) rather than being swallowed.
      await pushAllChatEntities(
        {
          threads: project.chatThreads,
          groups: project.chatThreadGroups,
          memberships: project.chatThreadGroupMemberships,
        },
        options,
      )
      await pushAllEditorEntities(
        {
          documents: project.editorDocuments,
          folders: project.editorFolders,
          comments: project.editorComments,
        },
        options,
      )
      await pushAllAssetEntities(
        {
          sections: project.fileLibrarySections,
          groups: project.fileGroups,
          assets: project.fileAssets,
        },
        options,
      )
      await pushAllVectorIndexEntities(project.vectorIndexes, options)
      dispatch({ enabled: true, type: 'setServerSyncEnabled' })
      preparedRef.current = null
    } finally {
      setImportPending(false)
    }
  }, [canPersist, dispatch, importPending, workspaceId])

  return { importPending, importToServer }
}
