import '@fontsource-variable/inter/index.css'
import '@/styles/globals.css'

import { useEffect, useMemo, useReducer } from 'react'
import { createRoot } from 'react-dom/client'

import { listAssets } from '@/api/inqtrixClient'
import { AppProviders } from '@/app/AppProviders'
import { initializeResearchDeskState, researchDeskReducer } from '@/features/researchDesk/state'
import { assetRecordFromServer } from '../assetSync'
import { useAssetHistoryApi } from '../useAssetHistoryApi'

/** The desk's own state and asset hook, with a consumer that asks for the
 * prepared bodies the way the chat does: from a list derived from the store,
 * so every store change hands it a new list. "Neu laden" reads the asset
 * metadata again, as a later sync pass does. */
function AssetBodyLoadFixture() {
  const [state, dispatch] = useReducer(researchDeskReducer, undefined, initializeResearchDeskState)
  const { bodyLoadStates, ensureAssetBodiesLoaded } = useAssetHistoryApi({
    apiKey: undefined,
    dispatch,
    fileAssets: state.fileAssets,
    fileGroups: state.fileGroups,
    fileLibrarySections: state.fileLibrarySections,
    projectEpoch: state.projectEpoch,
    syncActive: true,
    workspaceId: 'ws-fixture',
  })
  const pending = useMemo(() => Object.keys(state.fileAssets), [state.fileAssets])
  useEffect(() => {
    if (pending.length === 0) return
    void ensureAssetBodiesLoaded(pending).catch(() => undefined)
  }, [ensureAssetBodiesLoaded, pending])
  const status = bodyLoadStates['asset-unprepared']?.status ?? 'none'
  const reload = () => {
    void listAssets({}).then((page) => dispatch({
      assets: page.data.map(assetRecordFromServer),
      type: 'upsertServerAssetMetadata',
    }))
  }
  return (
    <div>
      <button onClick={reload} type="button">Neu laden</button>
      <div data-status={status} data-testid="asset-status">{status}</div>
    </div>
  )
}

createRoot(document.getElementById('root')!).render(
  <AppProviders>
    <AssetBodyLoadFixture />
  </AppProviders>,
)
