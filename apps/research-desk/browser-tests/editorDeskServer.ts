import type { Page, Route } from '@playwright/test'

// A stand-in for the editor endpoints of the service, enough for the desk's
// Markdown sync with a second writer: folders, the document list, a
// document's detail, its save with the service's revision guard, its
// metadata, its comments and the agent patches of a document. Every request
// that reaches the API is written to a log, so a test can say what the page
// sent, and nothing reaches a real server.

export type DeskDocument = {
  collaboration: boolean
  folderId: string | null
  id: string
  metadataRevision: number
  revision: number
  text: string
  title: string
  updatedAt: number
}

export type DeskPatchEdit = { find: string; id: string; note: string; text: string }

export type DeskPatch = {
  appliedEditIds: string[] | null
  documentId: string
  edits: DeskPatchEdit[]
  id: string
  status: 'accepted' | 'pending' | 'rejected'
}

export type DeskSave = { id: string; revision: number; text: string }

export type DeskServer = {
  documents: Map<string, DeskDocument>
  patches: DeskPatch[]
  /** Every request that reached the API, in order: `METHOD /path`. */
  requests: string[]
  /** The saves the service took, in order. */
  saves: DeskSave[]
  /** The saves the service refused with 409, in order: `id: reason`, the
   * reason `revision` for the revision guard. */
  refusals: string[]
  /** Another tab saves `text` as the next revision: a save of this page
   * on the revision before is refused with 409 by the revision guard. */
  writeElsewhere: (id: string, text: string) => void
  /** Another tab converts the document to collaboration: a Markdown save
   * of this page is refused with 409 and the reason
   * `content_mode_changed`. */
  convertElsewhere: (id: string) => void
  /** Holds the next request `matches` names (`METHOD /path`) until the
   * returned release runs. */
  hold: (matches: (request: string) => boolean) => () => void
  /** The requests of a method (`DELETE`, `PUT`, …), in order. */
  requestsOf: (method: string) => string[]
}

const COLLABORATION_WIRE = {
  comment_revision: 0,
  generation: 1,
  persisted_sequence: 0,
  projection_sequence: 0,
  projection_updated_at: null,
  schema_version: 1,
}

let clock = 0

/** A server time after every one before it, and after now, as another
 * tab's save is newer than anything this page did before. */
function nextServerTime(): number {
  clock = Math.max(clock + 1, Math.floor(Date.now() / 1000) + 1)
  return clock
}

export function deskDocument(id: string, title: string, text: string): DeskDocument {
  return {
    collaboration: false,
    folderId: null,
    id,
    metadataRevision: 1,
    revision: 1,
    text,
    title,
    updatedAt: nextServerTime(),
  }
}

/** A document as the service's detail returns it, with its text. */
export function deskDetail(server: DeskServer, id: string) {
  const document = server.documents.get(id)
  if (!document) throw new Error(`The desk server holds no document ${id}.`)
  return documentWire(document, true)
}

function documentWire(document: DeskDocument, withText: boolean) {
  return {
    access: { mode: 'owner', permission: 'edit' },
    ...(withText ? { content_markdown: document.text } : {}),
    collaboration: document.collaboration ? COLLABORATION_WIRE : null,
    content_mode: document.collaboration ? 'collaboration' : 'markdown',
    created_at: 1_760_000_000,
    diff_anchor_markdown: null,
    diff_anchor_updated_at: null,
    folder_id: document.folderId,
    id: document.id,
    metadata_revision: document.metadataRevision,
    revision: document.revision,
    source: 'blank',
    source_run_id: null,
    title: document.title,
    updated_at: document.updatedAt,
  }
}

function patchWire(server: DeskServer, patch: DeskPatch) {
  const document = server.documents.get(patch.documentId)
  return {
    applied_edit_ids: patch.appliedEditIds,
    applied_revision: patch.status === 'accepted' ? document?.revision ?? null : null,
    created_at: 1_760_000_000,
    decided_at: patch.status === 'pending' ? null : 1_760_000_100,
    document_id: patch.documentId,
    document_revision: document?.revision ?? 1,
    edit_count: patch.edits.length,
    edits: patch.edits.map(({ find, id, note, text }) => ({
      find,
      id,
      note,
      position: 'replace',
      quote_after: '',
      quote_before: '',
      text,
    })),
    note: '',
    patch_id: patch.id,
    revision_before: 1,
    run_id: 'run_desk',
    source: 'agent',
    status: patch.status,
    summary: 'Dokument aktualisiert',
    warnings: [],
  }
}

function conflict(message: string, extra: Record<string, unknown> = {}) {
  return { json: { error: { message, type: 'conflict', ...extra } }, status: 409 }
}

const DOCUMENT_PATH = /^\/v1\/editor\/documents\/([^/]+)$/
const COMMENTS_PATH = /^\/v1\/editor\/documents\/([^/]+)\/comments$/
const DOCUMENT_PATCHES_PATH = /^\/v1\/editor\/documents\/([^/]+)\/patches$/
const PATCH_PATH = /^\/v1\/editor\/patches\/([^/:]+)(:apply|:reject)?$/

/** Answers the editor endpoints for `documents` the way the service does. */
export async function serveDesk(page: Page, documents: DeskDocument[]): Promise<DeskServer> {
  const holds: Array<{ matches: (request: string) => boolean; released: Promise<void> }> = []
  const server: DeskServer = {
    convertElsewhere(id) {
      const document = stored(id)
      document.collaboration = true
      document.metadataRevision += 1
      document.updatedAt = nextServerTime()
    },
    documents: new Map(documents.map((document) => [document.id, { ...document }])),
    hold(matches) {
      let release = () => {}
      const released = new Promise<void>((resolve) => {
        release = resolve
      })
      holds.push({ matches, released })
      return release
    },
    patches: [],
    refusals: [],
    requests: [],
    requestsOf(method) {
      return server.requests.filter((request) => request.startsWith(`${method} `))
    },
    saves: [],
    writeElsewhere(id, text) {
      const document = stored(id)
      document.text = text
      document.revision += 1
      document.updatedAt = nextServerTime()
    },
  }

  function stored(id: string): DeskDocument {
    const document = server.documents.get(id)
    if (!document) throw new Error(`The desk server holds no document ${id}.`)
    return document
  }

  /** Logs the request and waits for a hold that names it. */
  async function arrive(route: Route): Promise<{ method: string; path: string }> {
    const request = route.request()
    const path = new URL(request.url()).pathname
    const entry = `${request.method()} ${path}`
    server.requests.push(entry)
    const index = holds.findIndex((hold) => hold.matches(entry))
    if (index >= 0) {
      const [hold] = holds.splice(index, 1)
      await hold.released
    }
    return { method: request.method(), path }
  }

  await page.route(
    (url) => url.pathname.startsWith('/v1/') || url.pathname.startsWith('/api/'),
    async (route) => {
      await arrive(route)
      await route.fulfill({ json: {}, status: 404 })
    },
  )
  await page.route(
    (url) => url.pathname === '/v1/editor/folders',
    async (route) => {
      await arrive(route)
      await route.fulfill({ json: { data: [] } })
    },
  )
  await page.route(
    (url) => url.pathname === '/v1/editor/documents',
    async (route) => {
      await arrive(route)
      // The list carries metadata only, as the service's list does.
      await route.fulfill({
        json: {
          data: [...server.documents.values()].map((document) => documentWire(document, false)),
          next_cursor: null,
        },
      })
    },
  )
  await page.route(
    (url) => COMMENTS_PATH.test(url.pathname),
    async (route) => {
      const { method } = await arrive(route)
      await route.fulfill({ json: method === 'GET' ? { data: [], next_cursor: null } : { data: [] } })
    },
  )
  await page.route(
    (url) => DOCUMENT_PATCHES_PATH.test(url.pathname),
    async (route) => {
      const { path } = await arrive(route)
      const [, documentId] = DOCUMENT_PATCHES_PATH.exec(path)!
      const asked = new URL(route.request().url()).searchParams.get('status')
      const listed = server.patches.filter((patch) => (
        patch.documentId === documentId && (asked === null || asked === patch.status)
      ))
      await route.fulfill({
        json: { data: listed.map((patch) => ({ patch_id: patch.id, source: 'agent', status: patch.status })) },
      })
    },
  )
  await page.route(
    (url) => PATCH_PATH.test(url.pathname),
    async (route) => {
      const { path } = await arrive(route)
      const [, patchId, decision] = PATCH_PATH.exec(path)!
      const patch = server.patches.find((item) => item.id === patchId)
      if (!patch) {
        await route.fulfill({ json: {}, status: 404 })
        return
      }
      if (decision === ':apply') {
        const body = route.request().postDataJSON() as { expected_revision?: number }
        const document = stored(patch.documentId)
        if (patch.status !== 'pending') {
          await route.fulfill(conflict('The patch already has a different decision.', { status: patch.status }))
          return
        }
        if (body.expected_revision !== document.revision) {
          await route.fulfill(conflict('Revision conflict.', { current_revision: document.revision }))
          return
        }
        // As the service applies a patch: every edit whose passage it finds.
        const applied: string[] = []
        for (const edit of patch.edits) {
          if (!document.text.includes(edit.find)) continue
          document.text = document.text.replace(edit.find, () => edit.text)
          applied.push(edit.id)
        }
        document.revision += 1
        document.updatedAt = nextServerTime()
        patch.appliedEditIds = applied
        patch.status = 'accepted'
        await route.fulfill({
          json: { applied_edit_ids: applied, document_id: document.id, revision: document.revision },
        })
        return
      }
      if (decision === ':reject') {
        patch.status = 'rejected'
      }
      await route.fulfill({ json: patchWire(server, patch) })
    },
  )
  await page.route(
    (url) => DOCUMENT_PATH.test(url.pathname),
    async (route) => {
      const { method, path } = await arrive(route)
      const [, id] = DOCUMENT_PATH.exec(path)!
      const existing = server.documents.get(id)
      if (method === 'GET') {
        await route.fulfill(existing
          ? { json: documentWire(existing, true) }
          : { json: { error: { message: 'Document not found', type: 'not_found' } }, status: 404 })
        return
      }
      if (method === 'DELETE') {
        server.documents.delete(id)
        await route.fulfill({ status: 204 })
        return
      }
      if (method === 'PATCH') {
        const body = route.request().postDataJSON() as { folder_id?: string | null; title?: string }
        const document = stored(id)
        if (body.title !== undefined) document.title = body.title
        if (body.folder_id !== undefined) document.folderId = body.folder_id
        document.metadataRevision += 1
        document.updatedAt = nextServerTime()
        await route.fulfill({ json: documentWire(document, true) })
        return
      }
      // A save, as the service takes it: it names the revision it creates,
      // which the service accepts only right after the one it holds. A
      // document in collaboration mode takes no Markdown save.
      const body = route.request().postDataJSON() as {
        content_markdown: string
        folder_id: string | null
        revision: number
        title: string
        updated_at: number
      }
      if (existing?.collaboration) {
        server.refusals.push(`${id}: content_mode_changed`)
        await route.fulfill(conflict('The document was converted to collaboration.', {
          reason: 'content_mode_changed',
        }))
        return
      }
      if (body.revision !== (existing?.revision ?? 0) + 1) {
        server.refusals.push(`${id}: revision`)
        await route.fulfill(conflict('The document has changed in the meantime.', {
          current_revision: existing?.revision ?? 0,
        }))
        return
      }
      const saved: DeskDocument = {
        collaboration: false,
        folderId: body.folder_id,
        id,
        metadataRevision: existing?.metadataRevision ?? 1,
        revision: body.revision,
        text: body.content_markdown,
        title: body.title,
        // The service keeps the time the save names.
        updatedAt: body.updated_at,
      }
      server.documents.set(id, saved)
      server.saves.push({ id, revision: saved.revision, text: saved.text })
      await route.fulfill({ json: documentWire(saved, true) })
    },
  )
  return server
}
