import { fetchActorJson } from './api.mjs'

let documentSequence = 0

export async function createCollaborationDocument({
  lifecycle,
  markdown,
  owner,
  runId,
  schemaVersion,
  title,
}) {
  documentSequence += 1
  const id = [
    'ed',
    runId,
    String(documentSequence).padStart(3, '0'),
    title.toLowerCase().replaceAll(/[^a-z0-9]+/g, '_'),
  ].join('_')
  const now = Date.now() / 1000
  const cleanupHandle = await lifecycle.register({
    credential: owner.credential,
    id,
    kind: 'document',
    ownerEmail: owner.email,
  })
  const saved = await fetchActorJson(
    owner,
    'PUT',
    `/v1/editor/documents/${id}`,
    {
      data: {
        content_markdown: markdown,
        created_at: now,
        diff_anchor_markdown: null,
        diff_anchor_updated_at: null,
        folder_id: null,
        revision: 1,
        source: 'blank',
        source_run_id: null,
        title,
        updated_at: now,
      },
    },
  )
  const collaboration = await fetchActorJson(
    owner,
    'POST',
    `/v1/editor/documents/${id}/collaboration:enable`,
    {
      data: {
        expected_metadata_revision: saved.metadata_revision,
        expected_revision: saved.revision,
        schema_version: schemaVersion,
      },
    },
  )
  return {
    cleanupHandle,
    generation: collaboration.generation,
    id,
    ownerId: owner.user.id,
    title,
  }
}

export async function deleteCollaborationDocument({
  lifecycle,
  owner,
  document,
}) {
  await fetchActorJson(
    owner,
    'DELETE',
    `/v1/editor/documents/${document.id}`,
    { expected: [204, 404] },
  )
  await lifecycle.completeDocumentCascade(document.id)
}
