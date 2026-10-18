import {
  suggestionCreatedAtMs,
  type CollaborationActorKind,
  type CollaborationChangeKind,
  type SuggestionDescriptor,
  type SuggestionKind,
} from '@inqtrix/editor-schema'
import type {
  EditorCollaborationConnectionStatus,
  EditorCollaborationDurabilityStatus,
} from '@/features/project/types'
import {
  collaborationNoticeCode,
  type CollaborationNotice,
  type CollaborationNoticeCode,
} from '../collaborationNotices'
import type {
  CollaborationEditRefusal,
  CollaborationEditRefusalReason,
} from '../tiptap'

export type EditorInspectorTab = 'assistant' | 'changes' | 'comments'
export type EditorChangesView = 'history' | 'open'
export type EditorWriteMode = 'comment' | 'edit' | 'suggest' | 'view'
export type InspectorHistoryKind = CollaborationChangeKind | 'comment'

export type InspectorParticipant = {
  color: string
  id: string
  name: string
}

export type EditorCollaborationStatusKind =
  | 'access_revoked'
  | 'error'
  | 'inactive'
  | 'origin_rejected'
  | 'read_only'
  | 'reconnecting'
  | 'saved'
  | 'saving'
  | 'saving_delayed'
  | 'syncing'
  | 'reload_required'

export type EditorCollaborationStatusModel = {
  active: boolean
  hasUnconfirmedLocalChanges: boolean
  kind: EditorCollaborationStatusKind
  nextReconnectAt: number | null
  notice: string | null
  participantOverflow: number
  participants: readonly InspectorParticipant[]
  projectionConfirmedAt: string | null
  reconnectAttempt: number
  recoverability: 'login' | 'none' | 'reload' | 'retry'
  /** When the oldest local change the service has not confirmed yet was
   * made (epoch milliseconds); null when none waits or the source does not
   * report it. */
  unconfirmedSince: number | null
  visibleParticipants: readonly InspectorParticipant[]
}

/**
 * The German text of every collaboration notice, by code. The type keeps the
 * table complete: a notice without a German text does not compile.
 */
export const GERMAN_COLLABORATION_NOTICES: Readonly<Record<CollaborationNoticeCode, string>> = {
  access_changed_revalidating:
    'Der Kollaborationszugriff hat sich geändert. Inqtrix prüft die aktuelle Berechtigung erneut.',
  access_changed_with_unconfirmed_updates:
    'Der Kollaborationszugriff hat sich geändert, bevor lokale Änderungen dauerhaft bestätigt waren. Ihr lokaler Stand bleibt für Wiederherstellung oder Sicherung erhalten.',
  access_unavailable:
    'Der Zugriff auf dieses Kollaborationsdokument ist nicht mehr verfügbar.',
  ack_delayed:
    'Der Kollaborationsdienst hat die letzte Änderung seit mehr als zehn Sekunden nicht bestätigt. Ihre Änderungen bleiben in diesem Editor erhalten.',
  authentication_failed:
    'Die Kollaborationssitzung konnte nicht bestätigt werden. Inqtrix stellt die Verbindung automatisch wieder her.',
  causal_gap_resending:
    'Dem Kollaborationsdienst fehlen frühere Änderungen aus diesem Editor. Inqtrix sendet sie in der richtigen Reihenfolge erneut.',
  causal_gap_unrecoverable:
    'Frühere Änderungen aus diesem Editor ließen sich nicht in der richtigen Reihenfolge wiederherstellen. Ihr lokaler Stand bleibt als Sicherung erhalten, bis Sie das Dokument neu laden.',
  connection_interrupted:
    'Die Verbindung zum Kollaborationsdienst wurde unterbrochen. Inqtrix verbindet sich automatisch erneut.',
  durability_failed_before_navigation:
    'Die Änderungen dieses Dokuments konnten vor dem Verlassen nicht dauerhaft gespeichert werden.',
  flush_interval_incompatible:
    'Das Sendeintervall des Kollaborationsdienstes ist mit dieser App-Version nicht kompatibel.',
  identity_changed:
    'Die Identität der Kollaborationssitzung hat sich geändert. Der Zugriff wurde vorsorglich beendet.',
  ingress_overflow_reconnecting:
    'Der Kollaborationsdienst hat Änderungen aus diesem Editor schneller erhalten, als er sie verarbeiten konnte. Inqtrix verbindet sich neu und sendet sie erneut.',
  lease_refresh_failed:
    'Die Verbindung zum Kollaborationsdienst ist unterbrochen. Inqtrix verbindet sich automatisch erneut.',
  limits_incompatible:
    'Die Grenzwerte des Kollaborationsdienstes sind mit dieser App-Version nicht kompatibel.',
  origin_rejected:
    'Der Kollaborationsdienst hat die Adresse dieser Seite abgelehnt. Die Adresse im Browser stimmt nicht mit der konfigurierten öffentlichen Adresse des Servers überein.',
  protocol_incompatible:
    'Diese App-Version ist nicht mit dem Kollaborationsprotokoll kompatibel.',
  protocol_or_schema_incompatible:
    'Diese App-Version ist nicht mit dem Kollaborationsprotokoll oder Dokumentschema kompatibel.',
  refresh_interval_incompatible:
    'Das Erneuerungsintervall der Kollaborationssitzung ist mit dieser App-Version nicht kompatibel.',
  release_unconfirmed:
    'Die Änderungen dieses Dokuments konnten vor dem Verlassen nicht bestätigt werden.',
  room_changed:
    'Der Kollaborationsraum hat sich beim Erneuern der Sitzung geändert. Laden Sie die Seite neu, um weiterzuarbeiten.',
  schema_unsupported:
    'Diese App-Version unterstützt das Dokumentschema nicht.',
  send_failed:
    'Lokale Änderungen konnten nicht an den Kollaborationsdienst gesendet werden. Inqtrix verbindet sich automatisch erneut.',
  session_ended:
    'Der Kollaborationsdienst hat diese Sitzung beendet und nimmt sie mit dem aktuellen Stand nicht wieder auf. Laden Sie die Seite neu, um weiterzuarbeiten.',
  session_expired:
    'Die Anmeldung ist abgelaufen. Melden Sie sich erneut an, um fortzufahren.',
  session_open_failed_retrying:
    'Die Kollaborationssitzung konnte nicht geöffnet werden. Inqtrix versucht es automatisch erneut.',
  session_open_refused:
    'Der Kollaborationsdienst hat das Öffnen dieser Sitzung abgelehnt.',
  update_rejected:
    'Der Kollaborationsdienst hat eine Änderung aus diesem Editor abgelehnt. Ihr lokaler Stand bleibt als Sicherung erhalten, bis Sie das Dokument neu laden.',
  update_too_large:
    'Der Kollaborationsdienst hat eine Änderung aus diesem Editor abgelehnt, weil sie die unterstützte Größe überschreitet. Ihr lokaler Stand bleibt als Sicherung erhalten, bis Sie das Dokument neu laden.',
  update_untracked:
    'Eine Änderung ließ sich nicht für die dauerhafte Speicherung vormerken.',
}

/** The text of a collaboration notice in the reader's language. */
export function localizeEditorCollaborationNotice(
  notice: CollaborationNotice | null,
  locale: 'de' | 'en',
): string | null {
  if (!notice || locale !== 'de') return notice
  const code = collaborationNoticeCode(notice)
  if (code === null) {
    console.warn('A collaboration notice has no code and is shown untranslated.', notice)
    return notice
  }
  return GERMAN_COLLABORATION_NOTICES[code]
}

/**
 * What the surface says when the editor refused a local change, by reason.
 * Every reason has a text in both languages; the type keeps the table
 * complete.
 */
export const COLLABORATION_EDIT_REFUSAL_COPY: Readonly<Record<
  CollaborationEditRefusalReason,
  { de: string; en: string }
>> = {
  adjacent_own_suggestion_conflict: {
    de: 'Diese Änderung schließt direkt an einen anderen offenen Vorschlag von Ihnen an und lässt sich nicht mit ihm verbinden. Ändern Sie an anderer Stelle.',
    en: 'This change sits directly next to another of your pending suggestions and cannot be joined with it. Make the change elsewhere.',
  },
  collaborator_required: {
    de: 'Änderungen sind erst möglich, wenn Ihre Identität im gemeinsamen Dokument bestätigt ist.',
    en: 'Changes are possible once your identity in the shared document is confirmed.',
  },
  divider_unsupported: {
    de: 'Eine Trennlinie lässt sich nicht als Vorschlag einfügen.',
    en: 'A divider cannot be added as a suggestion.',
  },
  foreign_collision: {
    de: 'Diese Bearbeitung überschneidet sich mit einer Änderung einer anderen Person. Prüfen Sie die markierte Änderung zuerst.',
    en: 'This edit overlaps a change from another collaborator. Review the selected change first.',
  },
  foreign_suggestion_changed: {
    de: 'Diese Änderung würde einen offenen Vorschlag einer anderen Person verändern. Ändern Sie an anderer Stelle oder warten Sie, bis über den Vorschlag entschieden ist.',
    en: 'This change would alter a pending suggestion of another collaborator. Make the change elsewhere or wait until the suggestion is decided.',
  },
  format_overlaps_own_suggestion: {
    de: 'Diese Formatierung würde Ihren eigenen offenen Vorschlag verändern. Formatieren Sie nur innerhalb Ihres Vorschlags oder nur außerhalb davon.',
    en: 'This formatting would alter your own pending suggestion. Format only inside your suggestion or only outside it.',
  },
  node_marks_unsupported: {
    de: 'Diese Änderung betrifft einen ganzen Block oder ein eingebettetes Element und lässt sich nicht als Vorschlag darstellen.',
    en: 'This change affects a whole block or an embedded element and cannot be shown as a suggestion.',
  },
  own_suggestion_edit_unsupported: {
    de: 'Dieser Text gehört zu Ihrem offenen Vorschlag. Entscheiden Sie zuerst über den Vorschlag oder wechseln Sie zu „Vorschlagen“.',
    en: 'This text belongs to your pending suggestion. Decide the suggestion first or switch to “Suggest”.',
  },
  proposed_block_unsupported: {
    de: 'Im Modus „Vorschlagen“ kann ein neuer Block nur ein Absatz, ein Listenpunkt oder ein offener Aufgabenpunkt sein.',
    en: 'In “Suggest” mode a new block can only be a paragraph, a list item or an open task item.',
  },
  split_moves_content: {
    de: 'Im Modus „Vorschlagen“ kann ein neuer Absatz nur am Ende eines Absatzes beginnen. Setzen Sie den Cursor ans Absatzende.',
    en: 'In “Suggest” mode a new paragraph can only start at the end of a paragraph. Place the cursor at the end of the paragraph.',
  },
  structure_suggestion_inapplicable: {
    de: 'Diese Änderung würde einen offenen Strukturvorschlag unanwendbar machen, etwa eine Formatierung in einem als Codeblock vorgeschlagenen Absatz.',
    en: 'This change would make a pending structural suggestion impossible to apply, for example formatting inside a paragraph proposed as a code block.',
  },
  structure_target_unsupported: {
    de: 'Für diesen Block lässt sich diese Strukturänderung nicht vorschlagen.',
    en: 'This structural change cannot be suggested for this block.',
  },
  suggestion_metadata_invalid: {
    de: 'Diese Änderung lässt sich nicht als Vorschlag speichern, weil ein Vorschlag im Dokument unvollständig ist.',
    en: 'This change cannot be saved as a suggestion because a suggestion in the document is incomplete.',
  },
  suggestion_undo_failed: {
    de: 'Ihr letzter Vorschlag konnte nicht zurückgezogen werden. Versuchen Sie es erneut.',
    en: 'Your last suggestion could not be withdrawn. Try again.',
  },
  suggestion_would_disappear: {
    de: 'Damit würde Ihr eigener offener Vorschlag ganz entfernt. Ein Vorschlag lässt sich nur zurückziehen, nicht löschen.',
    en: 'This would remove your own pending suggestion completely. A suggestion can only be withdrawn, not deleted.',
  },
  table_topology_unsupported: {
    de: 'Zeilen, Spalten und Zellen einer Tabelle lassen sich nicht als Vorschlag hinzufügen, entfernen, verbinden oder teilen.',
    en: 'Rows, columns and cells of a table cannot be added, removed, merged or split as a suggestion.',
  },
  unknown: {
    de: 'Diese Änderung konnte nicht übernommen werden.',
    en: 'This change could not be applied.',
  },
  unsupported_structure: {
    de: 'Diese Änderung lässt sich nicht als Vorschlag darstellen.',
    en: 'This change cannot be shown as a suggestion.',
  },
}

/**
 * What an AI change card says when publishing it into the shared document
 * was refused. The API's message is English by contract; the card names the
 * refusal by the API's reason in the reader's language, and every refusal
 * without a text of its own by `failed`. A refused structure uses
 * `COLLABORATION_EDIT_REFUSAL_COPY.unsupported_structure`, the text a typed
 * change gets for the same cause.
 */
export const COLLABORATION_PUBLICATION_REFUSAL_COPY = {
  conflict: {
    de: 'Der Vorschlag lässt sich nicht in das gemeinsame Dokument übernehmen, weil er mit dessen aktuellem Stand oder einem offenen Vorschlag kollidiert.',
    en: 'The suggestion cannot be added to the shared document because it conflicts with its current state or with a pending suggestion.',
  },
  failed: {
    de: 'Der Vorschlag konnte nicht in das gemeinsame Dokument übernommen werden.',
    en: 'The suggestion could not be added to the shared document.',
  },
  invalidSchema: {
    de: 'Der Vorschlag enthält eine Struktur, die das gemeinsame Dokument nicht speichern kann.',
    en: 'The suggestion contains a structure the shared document cannot store.',
  },
  stale: {
    de: 'Das gemeinsame Dokument hat sich inzwischen geändert. Versuchen Sie es erneut.',
    en: 'The shared document has changed in the meantime. Try again.',
  },
  tooLarge: {
    de: 'Der Vorschlag ist zu groß für das gemeinsame Dokument.',
    en: 'The suggestion is too large for the shared document.',
  },
} as const

/** Shown instead of the open changes when one pending suggestion of the
 * document lacks complete metadata and none can be listed. */
export const COLLABORATION_CHANGES_UNREADABLE_COPY = {
  de: 'Die offenen Änderungen lassen sich nicht anzeigen, weil ein Vorschlag im Dokument unvollständig ist.',
  en: 'The open changes cannot be shown because a suggestion in the document is incomplete.',
} as const

export function localizeCollaborationEditRefusal(
  refusal: CollaborationEditRefusal | null,
  locale: 'de' | 'en',
): string | null {
  if (!refusal) return null
  return COLLABORATION_EDIT_REFUSAL_COPY[refusal.reason][locale]
}

export type PendingCollaborationPublicationFocus = {
  documentId: string
  patchId: string
}

export type PendingCollaborationPublicationFocusByDocument = Readonly<
  Record<string, PendingCollaborationPublicationFocus>
>

type CollaborationStatusInput = {
  access: 'comment' | 'edit' | 'suggest' | 'view' | null
  active: boolean
  canEdit: boolean
  connectionStatus: EditorCollaborationConnectionStatus
  durabilityDelayed?: boolean
  durabilityStatus: EditorCollaborationDurabilityStatus
  hasUnconfirmedLocalChanges?: boolean
  nextReconnectAt?: number | null
  notice?: string | null
  participants: readonly InspectorParticipant[]
  projectionUpdatedAt?: string | null
  reconnectAttempt?: number
  recoverability?: 'login' | 'none' | 'reload' | 'retry'
  synced: boolean
  unconfirmedSince?: number | null
}

export function buildEditorCollaborationStatusModel({
  access,
  active,
  canEdit,
  connectionStatus,
  durabilityDelayed = false,
  durabilityStatus,
  hasUnconfirmedLocalChanges = false,
  nextReconnectAt = null,
  notice = null,
  participants,
  projectionUpdatedAt = null,
  reconnectAttempt = 0,
  recoverability = 'none',
  synced,
  unconfirmedSince = null,
}: CollaborationStatusInput): EditorCollaborationStatusModel {
  const preview = participantPreview(participants)
  let kind: EditorCollaborationStatusKind = 'saved'
  if (!active) kind = 'inactive'
  else if (connectionStatus === 'access_revoked') kind = 'access_revoked'
  else if (connectionStatus === 'incompatible') kind = 'reload_required'
  else if (connectionStatus === 'origin_rejected') kind = 'origin_rejected'
  else if (connectionStatus === 'error' || durabilityStatus === 'error') kind = 'error'
  else if (connectionStatus === 'reconnecting') kind = 'reconnecting'
  else if (connectionStatus === 'connecting' || !synced) kind = 'syncing'
  else if (connectionStatus === 'read_only' || access === 'view' || !canEdit) kind = 'read_only'
  else if (durabilityStatus === 'pending') kind = durabilityDelayed ? 'saving_delayed' : 'saving'

  const projectionConfirmedAt = projectionUpdatedAt
    && !Number.isNaN(new Date(projectionUpdatedAt).getTime())
    ? new Date(projectionUpdatedAt).toISOString()
    : null

  return {
    active,
    hasUnconfirmedLocalChanges,
    kind,
    nextReconnectAt,
    notice,
    participantOverflow: preview.overflow,
    participants,
    projectionConfirmedAt,
    reconnectAttempt,
    recoverability,
    unconfirmedSince,
    visibleParticipants: preview.visible,
  }
}

export function beginCollaborationPublicationFocus(
  documentId: string,
  patchId: string,
): PendingCollaborationPublicationFocus {
  return { documentId, patchId }
}

export function registerCollaborationPublicationFocus(
  pendingByDocument: PendingCollaborationPublicationFocusByDocument,
  documentId: string,
  patchId: string,
): PendingCollaborationPublicationFocusByDocument {
  return {
    ...pendingByDocument,
    [documentId]: beginCollaborationPublicationFocus(documentId, patchId),
  }
}

export function pendingCollaborationPublicationFocusForDocument(
  pendingByDocument: PendingCollaborationPublicationFocusByDocument,
  documentId: string | null,
): PendingCollaborationPublicationFocus | null {
  return documentId ? pendingByDocument[documentId] ?? null : null
}

export function isCollaborationPublicationFocusCurrent(
  pending: PendingCollaborationPublicationFocus,
  documentId: string | null,
): boolean {
  return pending.documentId === documentId
}

export function consumeCollaborationPublicationFocus(
  pending: PendingCollaborationPublicationFocus | null,
  documentId: string | null,
  changes: readonly InspectorChange[],
): {
  focusId: string | null
  pending: PendingCollaborationPublicationFocus | null
} {
  if (!pending || !isCollaborationPublicationFocusCurrent(pending, documentId)) {
    return { focusId: null, pending }
  }
  const publishedChange = changes.find((change) => change.id === pending.patchId)
  if (!publishedChange) return { focusId: null, pending }
  return { focusId: publishedChange.id, pending: null }
}

export type InspectorSuggestionExcerpt = {
  deletionText: string
  insertionText: string
  modificationText: string
  position: number
}

export type InspectorChange = {
  author: InspectorParticipant
  createdAt: number
  id: string
  originalText: string
  position: number
  proposedText: string
  suggestionIds: string[]
  type: SuggestionKind
}

export type InspectorHistoryEntry = {
  actor: InspectorParticipant
  actorKind: CollaborationActorKind
  commandId: string | null
  commentAction?: 'created' | 'message_deleted' | 'message_edited' | 'reopened' | 'replied' | 'resolved'
  createdAt: number
  fromSequence: number
  id: string
  outcome?: 'accepted' | 'rejected' | null
  summary?: Array<{
    after: string
    before: string
    kind: SuggestionKind | 'direct'
    position: number
  }>
  omittedEditCount?: number
  /** Pending suggestions this edit removed by deleting the text they
   * proposed: all of them counted, up to twenty named with their authors. */
  removedSuggestionCount?: number
  removedSuggestions?: InspectorRemovedSuggestion[]
  suggestionIds: string[]
  toSequence: number
  type: InspectorHistoryKind
  updateCount?: number
}

export type InspectorRemovedSuggestion = {
  author: InspectorParticipant
  patchId: string
  suggestionId: string
}

export type InspectorOpenFilters = {
  authorId: string | null
  type: SuggestionKind | null
}

export type InspectorHistoryFilters = {
  actorId: string | null
  type: InspectorHistoryKind | null
}

export function buildInspectorChanges(
  descriptors: readonly SuggestionDescriptor[],
  excerpts: ReadonlyMap<string, InspectorSuggestionExcerpt>,
  participants: readonly InspectorParticipant[],
  durableSuggestionAuthors: ReadonlyMap<string, InspectorParticipant> = new Map(),
): InspectorChange[] {
  const participantById = new Map(participants.map((participant) => [participant.id, participant]))
  const patches = new Map<string, SuggestionDescriptor[]>()
  for (const descriptor of descriptors) {
    const existing = patches.get(descriptor.patchId)
    if (existing) existing.push(descriptor)
    else patches.set(descriptor.patchId, [descriptor])
  }

  return [...patches.entries()].map(([patchId, patchDescriptors]) => {
    const first = patchDescriptors[0]
    if (!first) throw new Error(`Patch ${patchId} has no suggestions`)
    const patchExcerpts = patchDescriptors.map((descriptor) => excerpts.get(descriptor.suggestionId))
    const originalText = joinExcerptText(patchExcerpts, 'deletionText')
    const insertedText = joinExcerptText(patchExcerpts, 'insertionText')
    const modifiedText = joinExcerptText(patchExcerpts, 'modificationText')
    const positions = patchExcerpts
      .map((excerpt) => excerpt?.position)
      .filter((position): position is number => typeof position === 'number')
    const durableAuthor = patchDescriptors
      .map((descriptor) => durableSuggestionAuthors.get(descriptor.suggestionId))
      .find((participant) => participant !== undefined)
    return {
      author: durableAuthor ?? participantById.get(first.authorId) ?? {
        color: '#6b7280',
        id: first.authorId,
        name: 'Collaborator',
      },
      createdAt: suggestionCreatedAtMs(first.createdAt),
      id: patchId,
      originalText: originalText || modifiedText,
      position: positions.length > 0 ? Math.min(...positions) : Number.MAX_SAFE_INTEGER,
      proposedText: insertedText || modifiedText,
      suggestionIds: patchDescriptors.map((descriptor) => descriptor.suggestionId).sort(),
      type: patchType(patchDescriptors.map((descriptor) => descriptor.kind)),
    }
  }).sort((left, right) => (
    left.position - right.position
    || left.createdAt - right.createdAt
    || left.id.localeCompare(right.id)
  ))
}

function joinExcerptText(
  excerpts: readonly (InspectorSuggestionExcerpt | undefined)[],
  key: 'deletionText' | 'insertionText' | 'modificationText',
): string {
  return excerpts
    .map((excerpt) => excerpt?.[key].trim() ?? '')
    .filter(Boolean)
    .join(' ')
}

function patchType(types: readonly SuggestionKind[]): SuggestionKind {
  const distinct = new Set(types)
  if (distinct.size === 1) return types[0] ?? 'replacement'
  if (distinct.has('structure')) return 'structure'
  if (distinct.has('format')) return 'format'
  return 'replacement'
}

export function filterInspectorChanges(
  changes: readonly InspectorChange[],
  filters: InspectorOpenFilters,
): InspectorChange[] {
  return changes.filter((change) => (
    (filters.authorId === null || change.author.id === filters.authorId)
    && (filters.type === null || change.type === filters.type)
  ))
}

export function filterInspectorHistory(
  history: readonly InspectorHistoryEntry[],
  filters: InspectorHistoryFilters,
): InspectorHistoryEntry[] {
  return history.filter((entry) => (
    (filters.actorId === null || entry.actor.id === filters.actorId)
    && (filters.type === null || entry.type === filters.type)
  ))
}

export function participantPreview(
  participants: readonly InspectorParticipant[],
  maximum = 3,
): { overflow: number; visible: InspectorParticipant[] } {
  const limit = Math.max(0, Math.floor(maximum))
  return {
    overflow: Math.max(0, participants.length - limit),
    visible: participants.slice(0, limit),
  }
}

export function adjacentChangeId(
  changes: readonly InspectorChange[],
  selectedId: string | null,
  direction: -1 | 1,
): string | null {
  if (changes.length === 0) return null
  const selectedIndex = selectedId === null
    ? (direction === 1 ? -1 : changes.length)
    : changes.findIndex((change) => change.id === selectedId)
  if (selectedIndex < 0 && selectedId !== null) {
    return direction === 1 ? changes[0]?.id ?? null : changes.at(-1)?.id ?? null
  }
  const nextIndex = Math.max(0, Math.min(changes.length - 1, selectedIndex + direction))
  return changes[nextIndex]?.id ?? null
}

export function compactChangeText(change: InspectorChange): string {
  return (change.proposedText || change.originalText).replace(/\s+/g, ' ').trim()
}

export function effectiveEditorWriteMode(
  access: 'comment' | 'edit' | 'suggest' | 'view' | null,
  canEdit: boolean,
  requested: Exclude<EditorWriteMode, 'view'>,
): EditorWriteMode {
  if (!canEdit || access === null || access === 'view') return 'view'
  if (access === 'comment') return 'comment'
  if (access === 'suggest') {
    return requested === 'comment' ? 'comment' : 'suggest'
  }
  return requested
}

export function isOwnedEditorDocument(
  document: { access?: { mode: 'owner' | 'shared' } },
): boolean {
  return document.access?.mode !== 'shared'
}

/** What a document row IS, for the rail's interaction grammar:
 * - `owned-private`: mine, not in collaboration — there is no details page for
 *   it, so the row offers no details action.
 * - `owned-shared`: mine and in collaboration — details (access, activity)
 *   exist and the row marks it as shared.
 * - `shared-with-me`: someone else's document shared to me.
 *
 * Derived from exactly the facts that gate the details dialog, so the visible
 * affordance and the destination can never drift apart. A document that
 * carries neither field (a local file, a legacy save) reads as private. */
export type EditorDocumentRowKind = 'owned-private' | 'owned-shared' | 'shared-with-me'

export function editorDocumentRowKind(
  document: { access?: { mode: 'owner' | 'shared' }; contentMode?: string },
): EditorDocumentRowKind {
  if (!isOwnedEditorDocument(document)) return 'shared-with-me'
  return document.contentMode === 'collaboration' ? 'owned-shared' : 'owned-private'
}

export type EditorDocumentRailCapabilities = {
  canDelete: boolean
  canDrag: boolean
  canOpenDetails: boolean
  canPin: boolean
  canRename: boolean
  leadingRole: 'file' | 'people'
}

/** One semantic source for the editor rail's leading role and actions.
 * Rendering code supplies labels/icons, but it may not independently infer
 * ownership or collaboration affordances. */
export function editorDocumentRailCapabilities(
  document: { access?: { mode: 'owner' | 'shared' }; contentMode?: string },
): EditorDocumentRailCapabilities {
  const kind = editorDocumentRowKind(document)
  if (kind === 'shared-with-me') {
    return {
      canDelete: false,
      canDrag: false,
      canOpenDetails: true,
      canPin: false,
      canRename: false,
      leadingRole: 'people',
    }
  }
  return {
    canDelete: true,
    canDrag: true,
    canOpenDetails: kind === 'owned-shared',
    canPin: true,
    canRename: true,
    leadingRole: kind === 'owned-shared' ? 'people' : 'file',
  }
}

export function partitionEditorDocumentsByAccess<
  Document extends { access?: { mode: 'owner' | 'shared' } },
>(documents: readonly Document[]): { owned: Document[]; shared: Document[] } {
  const owned: Document[] = []
  const shared: Document[] = []
  for (const document of documents) {
    if (isOwnedEditorDocument(document)) owned.push(document)
    else shared.push(document)
  }
  return { owned, shared }
}

/** How long the startup transients may present CALM before they earn color.
 *
 * Opening a document legitimately passes inactive -> syncing -> saved within
 * a few hundred milliseconds. Showing that sequence verbatim flashes a gray
 * dot, an amber dot and three label swaps for a state nobody can act on.
 * Within this grace window both startup transients present as ONE quiet
 * "syncing" with a muted dot; a session still not up after the window shows
 * its real state — and every exceptional kind bypasses the calm entirely
 * (the useCalmCollaborationStatusKind philosophy, applied to startup).
 */
export const COLLABORATION_STARTUP_GRACE_MS = 1_200

const STARTUP_TRANSIENT_KINDS: ReadonlySet<EditorCollaborationStatusKind> = new Set([
  'inactive',
  'syncing',
])

export function startupPresentation(
  kind: EditorCollaborationStatusKind,
  sinceMountMs: number,
  collaborationExpected: boolean,
): { calm: boolean; kind: EditorCollaborationStatusKind } {
  // A local markdown document is FINAL `inactive` ("Lokal") — there is no
  // session coming, so the grace would show 1.2s of "syncing" for a state
  // that was already the truth. Only documents that actually start a
  // collaboration session get the calm window.
  if (!collaborationExpected) return { calm: false, kind }
  if (!STARTUP_TRANSIENT_KINDS.has(kind)) return { calm: false, kind }
  if (sinceMountMs >= COLLABORATION_STARTUP_GRACE_MS) return { calm: false, kind }
  return { calm: true, kind: 'syncing' }
}

/** How long the oldest unconfirmed local change waits before the status
 * says "Wird gespeichert". Younger changes leave it at "Gespeichert". */
export const COLLABORATION_SAVING_REVEAL_MS = 2_000
/** How long "Wird gespeichert" stays at least once it is shown. */
export const COLLABORATION_SAVING_MIN_VISIBLE_MS = 500

/**
 * The status kind a person sees for the kind the collaboration reports.
 *
 * A writer whose changes are confirmed within a second has something
 * unconfirmed almost all the time while typing; showing that as "Wird
 * gespeichert" flickers and says nothing a person can act on. So `saving`
 * shows as "Gespeichert" until the oldest unconfirmed change has waited
 * COLLABORATION_SAVING_REVEAL_MS; then "Wird gespeichert" stays until
 * everything is confirmed, for at least COLLABORATION_SAVING_MIN_VISIBLE_MS.
 * A delay ("Speichern verzögert") that ends while changes still wait
 * continues as "Wird gespeichert". Without a reported change time the wait
 * counts from the moment the status first saw a change unconfirmed. Every
 * other kind shows at once.
 */
export class CalmCollaborationStatus {
  private listener: ((kind: EditorCollaborationStatusKind) => void) | null = null
  /** When this status first saw the current unconfirmed changes. */
  private pendingSeenAt: number | null = null
  private savingShownAt: number | null
  private timer: ReturnType<typeof setTimeout> | null = null
  private visible: EditorCollaborationStatusKind

  constructor(kind: EditorCollaborationStatusKind, unconfirmedSince: number | null) {
    const now = Date.now()
    this.visible = kind === 'saving'
      && (unconfirmedSince ?? now) + COLLABORATION_SAVING_REVEAL_MS > now
      ? 'saved'
      : kind
    this.savingShownAt = this.visible === 'saving' ? now : null
  }

  get kind(): EditorCollaborationStatusKind {
    return this.visible
  }

  /** Reports every change of the shown kind to `listener` until the
   * returned function ends it, which also cancels a pending change. */
  listen(listener: (kind: EditorCollaborationStatusKind) => void): () => void {
    this.listener = listener
    return () => {
      if (this.listener === listener) this.listener = null
      this.clearTimer()
    }
  }

  /** The kind the collaboration reports now, and when its oldest
   * unconfirmed local change was made. */
  update(kind: EditorCollaborationStatusKind, unconfirmedSince: number | null): void {
    this.clearTimer()
    const now = Date.now()
    if (kind === 'saving' || kind === 'saving_delayed') this.pendingSeenAt ??= now
    else this.pendingSeenAt = null
    if (kind === 'saving') {
      // Shown once, it stays until everything is confirmed.
      if (this.visible === 'saving') return
      // A delay that ended while changes still wait: the saving goes on.
      if (this.visible === 'saving_delayed') {
        this.show('saving')
        return
      }
      const since = unconfirmedSince ?? this.pendingSeenAt ?? now
      const remaining = since + COLLABORATION_SAVING_REVEAL_MS - now
      if (remaining <= 0) {
        this.show('saving')
        return
      }
      this.show('saved')
      this.timer = setTimeout(() => {
        this.timer = null
        this.show('saving')
      }, remaining)
    } else if (kind === 'saved' && this.visible === 'saving') {
      const shownAt = this.savingShownAt ?? now
      const remaining = Math.max(
        0,
        COLLABORATION_SAVING_MIN_VISIBLE_MS - (now - shownAt),
      )
      if (remaining === 0) this.show('saved')
      else {
        this.timer = setTimeout(() => {
          this.timer = null
          this.show('saved')
        }, remaining)
      }
    } else if (kind !== this.visible) {
      this.show(kind)
    }
  }

  private show(kind: EditorCollaborationStatusKind): void {
    if (kind === this.visible) return
    this.visible = kind
    this.savingShownAt = kind === 'saving' ? Date.now() : null
    this.listener?.(kind)
  }

  private clearTimer(): void {
    if (this.timer === null) return
    clearTimeout(this.timer)
    this.timer = null
  }
}
