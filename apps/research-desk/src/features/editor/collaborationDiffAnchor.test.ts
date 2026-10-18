import { describe, expect, it, vi } from 'vitest'

import type { ServerEditorDocument } from '@/api/inqtrixClient'
import {
  beginCollaborationAuthorityGuard,
  type CollaborationLiveAuthority,
} from './collaborationAuthority'
import { persistCollaborationDiffAnchor } from './collaborationDiffAnchor'

function writableAuthority(
  overrides: Partial<CollaborationLiveAuthority> = {},
): CollaborationLiveAuthority {
  return {
    access: 'edit',
    blockingFailure: null,
    canEdit: true,
    connectionStatus: 'connected',
    documentId: 'document-a',
    generation: 3,
    lifecycleStatus: 'saved',
    revision: 7,
    synced: true,
    ...overrides,
  }
}

const savedDocument: ServerEditorDocument = {
  access: { mode: 'owner', permission: 'edit' },
  collaboration: {
    generation: 3,
    persisted_sequence: 12,
    projection_sequence: 12,
    projection_updated_at: 2,
    schema_version: 1,
  },
  content_markdown: '# Durable',
  content_mode: 'collaboration',
  created_at: 1,
  diff_anchor_markdown: '# Durable',
  diff_anchor_updated_at: 2,
  folder_id: null,
  id: 'document-a',
  metadata_revision: 5,
  revision: 2,
  source: 'blank',
  source_run_id: null,
  title: 'Draft',
  updated_at: 2,
}

describe('collaboration diff-anchor authority', () => {
  it('persists the confirmed projection and commits each authoritative metadata result', async () => {
    const authority = writableAuthority()
    const guard = beginCollaborationAuthorityGuard(
      { readAuthority: () => authority },
      { documentId: 'document-a', generation: 3 },
      'write',
    )
    const commits: string[] = []
    const patchMetadata = vi.fn(async () => savedDocument) as never

    await expect(persistCollaborationDiffAnchor({
      authorityGuard: guard,
      clientOptions: { workspaceId: 'workspace-1' },
      controller: {
        flushAndAwaitDurable: async () => 12,
        readAuthority: () => authority,
        setAuthoritativeSequence: vi.fn(),
      },
      documentId: 'document-a',
      expectedMetadataRevision: 4,
      flushProjection: async () => ({
        authoritative_sequence: 12,
        content_markdown: '# Durable',
        generation: 3,
        projection_hash: 'hash',
        sequence: 12,
      }),
      locale: 'en',
      onAdoptMetadataRevision: (revision) => commits.push(`revision:${revision}`),
      onDocumentSaved: (document) => commits.push(`document:${document.id}`),
      onServerDocumentObserved: (document) => commits.push(`observed:${document.id}`),
      patchMetadata,
    })).resolves.toMatchObject({ id: 'document-a', metadataRevision: 5 })

    expect(patchMetadata).toHaveBeenCalledWith(
      'document-a',
      expect.objectContaining({
        diff_anchor_markdown: '# Durable',
        expected_metadata_revision: 4,
      }),
      { workspaceId: 'workspace-1' },
    )
    expect(commits).toEqual([
      'revision:5',
      'document:document-a',
      'observed:document-a',
    ])
  })

  it('does not persist metadata or commit state for a projection from another generation', async () => {
    const authority = writableAuthority()
    const guard = beginCollaborationAuthorityGuard(
      { readAuthority: () => authority },
      { documentId: 'document-a', generation: 3 },
      'write',
    )
    const setAuthoritativeSequence = vi.fn()
    const patchMetadata = vi.fn()
    const onAdoptMetadataRevision = vi.fn()
    const onDocumentSaved = vi.fn()
    const onServerDocumentObserved = vi.fn()

    await expect(persistCollaborationDiffAnchor({
      authorityGuard: guard,
      clientOptions: {},
      controller: {
        flushAndAwaitDurable: async () => 12,
        readAuthority: () => authority,
        setAuthoritativeSequence,
      },
      documentId: 'document-a',
      expectedMetadataRevision: 4,
      flushProjection: async () => ({
        authoritative_sequence: 12,
        content_markdown: '# Recreated document',
        generation: 4,
        projection_hash: 'generation-4-hash',
        sequence: 12,
      }),
      locale: 'en',
      onAdoptMetadataRevision,
      onDocumentSaved,
      onServerDocumentObserved,
      patchMetadata: patchMetadata as never,
    })).rejects.toMatchObject({ code: 'projection_generation_mismatch' })

    expect(setAuthoritativeSequence).not.toHaveBeenCalled()
    expect(patchMetadata).not.toHaveBeenCalled()
    expect(onAdoptMetadataRevision).not.toHaveBeenCalled()
    expect(onDocumentSaved).not.toHaveBeenCalled()
    expect(onServerDocumentObserved).not.toHaveBeenCalled()
  })

  it.each([
    {
      label: 'downgrade',
      nextAuthority: writableAuthority({
        access: 'view',
        canEdit: false,
        connectionStatus: 'read_only',
        lifecycleStatus: 'read_only',
        revision: 8,
      }),
    },
    {
      label: 'document switch',
      nextAuthority: writableAuthority({
        documentId: 'document-b',
        generation: 4,
        revision: 8,
      }),
    },
  ])('commits nothing after a $label during the metadata await', async ({ nextAuthority }) => {
    let authority = writableAuthority()
    const authoritySource = { readAuthority: () => authority }
    const guard = beginCollaborationAuthorityGuard(
      authoritySource,
      { documentId: 'document-a', generation: 3 },
      'write',
    )
    const onAdoptMetadataRevision = vi.fn()
    const onDocumentSaved = vi.fn()
    const onServerDocumentObserved = vi.fn()
    const patchMetadata = vi.fn(async () => {
      authority = nextAuthority
      return savedDocument
    }) as never

    await expect(persistCollaborationDiffAnchor({
      authorityGuard: guard,
      clientOptions: {},
      controller: {
        flushAndAwaitDurable: async () => 12,
        readAuthority: () => authority,
        setAuthoritativeSequence: vi.fn(),
      },
      documentId: 'document-a',
      expectedMetadataRevision: 4,
      flushProjection: async () => ({
        authoritative_sequence: 12,
        content_markdown: '# Durable',
        generation: 3,
        projection_hash: 'hash',
        sequence: 12,
      }),
      locale: 'en',
      onAdoptMetadataRevision,
      onDocumentSaved,
      onServerDocumentObserved,
      patchMetadata,
    })).rejects.toThrow()

    expect(patchMetadata).toHaveBeenCalledOnce()
    expect(onAdoptMetadataRevision).not.toHaveBeenCalled()
    expect(onDocumentSaved).not.toHaveBeenCalled()
    expect(onServerDocumentObserved).not.toHaveBeenCalled()
  })
})
