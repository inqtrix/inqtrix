import { describe, expect, it, vi } from 'vitest'

import { exportAgentRunArtifact } from '@/api/inqtrixClient'
import { useAgentControlApi } from './useAgentControlApi'

vi.mock('react', async (original) => ({
  ...await original<typeof import('react')>(),
  useRef: <T,>(value: T) => ({ current: value }),
  useCallback: <T,>(callback: T) => callback,
  useMemo: <T,>(factory: () => T) => factory(),
  useEffect: () => {},
}))
vi.mock('@/api/inqtrixClient', async (original) => ({
  ...await original<typeof import('@/api/inqtrixClient')>(),
  exportAgentRunArtifact: vi.fn(),
}))

function control(refresh: () => void) {
  return useAgentControlApi({
    apiKey: undefined,
    workspaceId: 'owner-workspace',
    enabled: true,
    runs: {},
    selectedSessionId: null,
    dispatch: vi.fn(),
    onEditorDocumentsChanged: refresh,
  })
}

describe('artifact export refresh', () => {
  it('refreshes authoritative editor state only after the server created the copy', async () => {
    let resolve!: (value: Record<string, unknown>) => void
    const pending = new Promise<Record<string, unknown>>((done) => { resolve = done })
    vi.mocked(exportAgentRunArtifact).mockReturnValueOnce(pending)
    const refresh = vi.fn()
    const exported = control(refresh).exportArtifact('run', 'artifact', 'Report')
    expect(refresh).not.toHaveBeenCalled()
    expect(exportAgentRunArtifact).toHaveBeenLastCalledWith(
      'run', 'artifact', { target: 'editor_document', title: 'Report' },
      { apiKey: undefined, workspaceId: 'owner-workspace' },
    )
    const document = { id: 'new-document', title: 'Report' }
    resolve(document)
    expect(await exported).toBe(document)
    expect(refresh).toHaveBeenCalledOnce()
  })

  it('keeps a failed export visible to its caller and refreshes on a later successful retry', async () => {
    const failure = new Error('Export unavailable')
    vi.mocked(exportAgentRunArtifact)
      .mockRejectedValueOnce(failure)
      .mockResolvedValueOnce({ id: 'new-document' })
    const refresh = vi.fn()
    const api = control(refresh)
    await expect(api.exportArtifact('run', 'artifact')).rejects.toBe(failure)
    expect(refresh).not.toHaveBeenCalled()
    await api.exportArtifact('run', 'artifact')
    expect(refresh).toHaveBeenCalledOnce()
  })
})
