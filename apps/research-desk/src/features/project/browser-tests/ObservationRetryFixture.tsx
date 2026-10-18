import '@fontsource-variable/inter/index.css'
import '@/styles/globals.css'

import { useReducer, useState } from 'react'
import { createRoot } from 'react-dom/client'

import { listAssets } from '@/api/inqtrixClient'
import { AppProviders } from '@/app/AppProviders'
import { assetRecordFromServer } from '@/features/fileLibrary/assetSync'
import { useAssetDeletionApi } from '@/features/fileLibrary/useAssetDeletionApi'
import { useAssetHistoryApi } from '@/features/fileLibrary/useAssetHistoryApi'
import { initializeResearchDeskState, researchDeskReducer } from '@/features/researchDesk/state'
import type { SessionDeletionState } from '../sessionDeletion'
import type { FileAssetRecord } from '../types'
import { useSessionDeletionApi } from '../useSessionDeletionApi'

declare global {
  interface Window {
    /** The path of every request the page sent, in order. */
    __requestPaths?: string[]
  }
}

// Requests are counted where the page sends them: a read a page timer starts
// is listed the moment the timer fires, before any network hop.
const requestPaths: string[] = []
window.__requestPaths = requestPaths
const pageFetch = window.fetch.bind(window)
window.fetch = (input, init) => {
  const url = input instanceof Request ? input.url : String(input)
  requestPaths.push(new URL(url, window.location.href).pathname)
  return pageFetch(input, init)
}

function Observation({ error, stopped }: { error: string | null; stopped: boolean }) {
  return (
    <div data-stopped={String(stopped)} data-testid="observation">
      {error}
    </div>
  )
}

/** The desk's own asset hook: its upload status poll starts once the
 * library is hydrated. "Neu laden" reads the asset list again, as a later
 * sync pass does. */
function UploadObservation() {
  const [state, dispatch] = useReducer(researchDeskReducer, undefined, initializeResearchDeskState)
  const { error, observationStopped } = useAssetHistoryApi({
    apiKey: undefined,
    dispatch,
    fileAssets: state.fileAssets,
    fileGroups: state.fileGroups,
    fileLibrarySections: state.fileLibrarySections,
    projectEpoch: state.projectEpoch,
    syncActive: true,
    workspaceId: 'ws-fixture',
  })
  const reload = () => {
    void listAssets({}).then((page) => dispatch({
      assets: page.data.map(assetRecordFromServer),
      type: 'upsertServerAssetMetadata',
    }))
  }
  return (
    <div>
      <button onClick={reload} type="button">Neu laden</button>
      <Observation error={error} stopped={observationStopped} />
    </div>
  )
}

const NO_ROWS = {}
const SAME_ORIGIN = {}
const ignore = () => undefined

const DELETING_ASSETS: Record<string, FileAssetRecord> = {
  'asset-1': {
    createdAt: '2026-10-12T08:00:00.000Z', deletionOperationId: 'op-asset',
    extractedText: '', fileName: 'vertrag.pdf', groupId: null, id: 'asset-1',
    label: 'vertrag', lifecycleStatus: 'deleting', mimeType: 'application/pdf',
    origin: 'library', pageCount: null, parseStatus: 'parsed', parseWarning: null,
    sectionId: 'sec-library', sizeBytes: 1_024, textTruncated: false, title: 'Vertrag',
    updatedAt: '2026-10-12T08:00:00.000Z',
  },
}

/** The asset deletion hook with a row whose deletion runs: the hook polls
 * its receipt. "Zeilen ändern" hands the hook changed rows, as another
 * deletion does; "Löschstatus neu laden" reads the retained receipts again,
 * as a server notice does; "Projekt wechseln" opens another project without
 * rows. */
function AssetDeletionObservation() {
  const [assets, setAssets] = useState<Record<string, FileAssetRecord>>(DELETING_ASSETS)
  const [refreshToken, setRefreshToken] = useState(0)
  const [scopeKey, setScopeKey] = useState('project-1')
  const switchProject = () => {
    setScopeKey('project-2')
    setAssets({})
  }
  const { error, observationStopped } = useAssetDeletionApi({
    assets,
    dispatch: ignore,
    groups: NO_ROWS,
    options: SAME_ORIGIN,
    refreshToken,
    scopeKey,
    sections: NO_ROWS,
  })
  return (
    <div>
      <button onClick={() => setAssets((current) => ({ ...current }))} type="button">
        Zeilen ändern
      </button>
      <button onClick={() => setRefreshToken((token) => token + 1)} type="button">
        Löschstatus neu laden
      </button>
      <button onClick={switchProject} type="button">Projekt wechseln</button>
      <Observation error={error} stopped={observationStopped} />
    </div>
  )
}

const DELETING_SESSIONS: Record<string, { deletion: SessionDeletionState }> = {
  'session-1': {
    deletion: { error: null, operationId: 'op-session', stage: 'queued', status: 'deleting' },
  },
}
const noOperation = async () => null

/** The session deletion hook: a session the list shows as deleting has its
 * receipt polled. "Sitzungen ändern" hands the hook a changed list, as any
 * session edit does; "Projekt wechseln" opens another project without
 * sessions. */
function SessionDeletionObservation() {
  const [sessions, setSessions] = useState<Record<string, { deletion: SessionDeletionState }>>(
    DELETING_SESSIONS,
  )
  const [scopeKey, setScopeKey] = useState('project-1')
  const switchProject = () => {
    setScopeKey('project-2')
    setSessions({})
  }
  const { error, observationStopped } = useSessionDeletionApi({
    enabled: true,
    onComplete: ignore,
    onState: ignore,
    options: SAME_ORIGIN,
    scopeKey,
    sessions,
    start: noOperation,
    targetKind: 'agent_session',
  })
  return (
    <div>
      <button onClick={() => setSessions((current) => ({ ...current }))} type="button">
        Sitzungen ändern
      </button>
      <button onClick={switchProject} type="button">Projekt wechseln</button>
      <Observation error={error} stopped={observationStopped} />
    </div>
  )
}

const LOOPS = {
  'asset-deletion': AssetDeletionObservation,
  'session-deletion': SessionDeletionObservation,
  uploads: UploadObservation,
}
const Loop = LOOPS[new URLSearchParams(window.location.search).get('loop') as keyof typeof LOOPS]

createRoot(document.getElementById('root')!).render(
  <AppProviders>
    <Loop />
  </AppProviders>,
)
