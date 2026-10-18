import { describe, expect, it, vi } from 'vitest'

import type { ServerEditorDocument } from '@/api/inqtrixClient'
import { hydrateSharedEditorTarget } from './sharedEditorTarget'

const DETAIL: ServerEditorDocument = {
  access: { mode: 'shared', permission: 'suggest' },
  collaboration: {
    generation: 2,
    persisted_sequence: 11,
    projection_sequence: 11,
    projection_updated_at: 1_783_987_200,
    schema_version: 1,
  },
  content_markdown: '# Durable projection',
  content_mode: 'collaboration',
  created_at: 1_783_987_200,
  diff_anchor_markdown: null,
  diff_anchor_updated_at: null,
  folder_id: null,
  id: 'document-1',
  metadata_revision: 3,
  revision: 4,
  source: 'blank',
  source_run_id: null,
  title: 'Shared document',
  updated_at: 1_783_987_260,
}

describe('shared editor target hydration', () => {
  it('always uses exact document detail including collaboration fallback markdown', async () => {
    const load = vi.fn(async () => DETAIL)

    const document = await hydrateSharedEditorTarget(
      DETAIL.id,
      {},
      'en',
      load,
    )

    expect(load).toHaveBeenCalledWith(DETAIL.id, {})
    expect(document).toMatchObject({
      contentMarkdown: '# Durable projection',
      contentMode: 'collaboration',
      id: DETAIL.id,
    })
  })

  it('discards a completed older request after its target is aborted', async () => {
    const controller = new AbortController()
    let resolveDetail: ((value: ServerEditorDocument) => void) | undefined
    const load = () => new Promise<ServerEditorDocument>((resolve) => {
      resolveDetail = resolve
    })
    const hydration = hydrateSharedEditorTarget(
      DETAIL.id,
      { signal: controller.signal },
      'en',
      load,
    )

    controller.abort()
    resolveDetail?.(DETAIL)

    await expect(hydration).rejects.toMatchObject({ name: 'AbortError' })
  })

  it('rejects a metadata-only collaboration row as incomplete detail', async () => {
    const metadataOnly = { ...DETAIL }
    delete metadataOnly.content_markdown

    await expect(hydrateSharedEditorTarget(
      DETAIL.id,
      {},
      'en',
      async () => metadataOnly,
    )).rejects.toThrow('does not include a complete projection')
  })
})
