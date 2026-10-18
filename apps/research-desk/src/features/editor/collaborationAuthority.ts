import type { EditorCollaborationConnectionStatus } from '@/features/project/types'

export type CollaborationAuthorityRequirement = 'decision' | 'read' | 'write'

export type CollaborationDocumentIdentity = {
  documentId: string
  generation: number
}

export type CollaborationLiveAuthority = {
  access: 'comment' | 'edit' | 'suggest' | 'view' | null
  blockingFailure: string | null
  canEdit: boolean
  connectionStatus: EditorCollaborationConnectionStatus
  documentId: string | null
  generation: number | null
  lifecycleStatus: 'connecting' | 'error' | 'inactive' | 'read_only' | 'reconnecting' | 'saved' | 'syncing'
  revision: number
  synced: boolean
}

export type CollaborationAuthoritySource = {
  readAuthority: () => CollaborationLiveAuthority
}

export type CollaborationAuthorityGuard = {
  assertCurrent: () => CollaborationLiveAuthority
  identity: CollaborationDocumentIdentity
  requirement: CollaborationAuthorityRequirement
  revision: number
}

export type CollaborationAuthorityErrorCode =
  | 'access_forbidden'
  | 'authority_changed'
  | 'document_changed'
  | 'not_writable'

export class CollaborationAuthorityError extends Error {
  readonly code: CollaborationAuthorityErrorCode

  constructor(code: CollaborationAuthorityErrorCode, message: string) {
    super(message)
    this.code = code
    this.name = 'CollaborationAuthorityError'
  }
}

export function beginCollaborationAuthorityGuard(
  source: CollaborationAuthoritySource,
  identity: CollaborationDocumentIdentity,
  requirement: CollaborationAuthorityRequirement,
  locale: 'de' | 'en' = 'en',
): CollaborationAuthorityGuard {
  const initial = source.readAuthority()
  assertCollaborationAuthority(initial, identity, requirement, locale)
  const revision = initial.revision

  return {
    assertCurrent: () => {
      const current = source.readAuthority()
      assertCollaborationAuthority(current, identity, requirement, locale)
      if (current.revision !== revision) {
        throw new CollaborationAuthorityError(
          'authority_changed',
          locale === 'de'
            ? 'Der Kollaborationszugriff hat sich während des Vorgangs geändert. Bitte erneut versuchen.'
            : 'Collaboration access changed during the operation. Try again.',
        )
      }
      return current
    },
    identity,
    requirement,
    revision,
  }
}

export function collaborationAuthorityDisabledReason(
  source: CollaborationAuthoritySource,
  identity: CollaborationDocumentIdentity,
  requirement: CollaborationAuthorityRequirement,
  locale: 'de' | 'en',
): string | null {
  try {
    assertCollaborationAuthority(source.readAuthority(), identity, requirement, locale)
    return null
  } catch (error) {
    return error instanceof Error ? error.message : collaborationUnavailableMessage(locale)
  }
}

export function assertCollaborationAuthority(
  authority: CollaborationLiveAuthority,
  identity: CollaborationDocumentIdentity,
  requirement: CollaborationAuthorityRequirement,
  locale: 'de' | 'en' = 'en',
): void {
  if (
    authority.documentId !== identity.documentId
    || authority.generation !== identity.generation
  ) {
    throw new CollaborationAuthorityError(
      'document_changed',
      locale === 'de'
        ? 'Das aktive Kollaborationsdokument hat sich geändert. Bitte erneut versuchen.'
        : 'The active collaboration document changed. Try again.',
    )
  }
  if (authority.connectionStatus === 'access_revoked') {
    throw new CollaborationAuthorityError(
      'access_forbidden',
      collaborationUnavailableMessage(locale, authority.connectionStatus),
    )
  }
  if (requirement === 'read') {
    const readableLifecycle = (
      authority.connectionStatus === 'connected'
      && authority.lifecycleStatus === 'saved'
    ) || (
      authority.connectionStatus === 'read_only'
      && authority.lifecycleStatus === 'read_only'
    )
    if (
      authority.access === null
      || !authority.synced
      || !readableLifecycle
      || authority.blockingFailure !== null
    ) {
      throw new CollaborationAuthorityError(
        'not_writable',
        collaborationUnavailableMessage(locale, authority.connectionStatus),
      )
    }
    return
  }
  if (requirement === 'decision' && authority.access !== 'edit') {
    throw new CollaborationAuthorityError(
      'access_forbidden',
      locale === 'de'
        ? 'Nur Personen mit Bearbeitungszugriff können geteilte Änderungen entscheiden.'
        : 'Only editors can decide shared changes.',
    )
  }
  if (requirement === 'write' && authority.access !== 'edit' && authority.access !== 'suggest') {
    throw new CollaborationAuthorityError(
      'access_forbidden',
      locale === 'de'
        ? 'Dieser Kollaborationszugriff ist schreibgeschützt.'
        : 'This collaboration access is read-only.',
    )
  }
  if (
    !authority.canEdit
    || !authority.synced
    || authority.connectionStatus !== 'connected'
    || authority.lifecycleStatus !== 'saved'
    || authority.blockingFailure !== null
  ) {
    throw new CollaborationAuthorityError(
      'not_writable',
      collaborationUnavailableMessage(locale, authority.connectionStatus),
    )
  }
}

function collaborationUnavailableMessage(
  locale: 'de' | 'en',
  connectionStatus?: EditorCollaborationConnectionStatus,
): string {
  if (connectionStatus === 'reconnecting' || connectionStatus === 'connecting') {
    return locale === 'de'
      ? 'Die Kollaboration wird neu verbunden. Schreibaktionen sind vorübergehend nicht verfügbar.'
      : 'Collaboration is reconnecting. Editing actions are temporarily unavailable.'
  }
  if (connectionStatus === 'origin_rejected') {
    return locale === 'de'
      ? 'Der Kollaborationsserver hat die Adresse dieser Seite abgelehnt. Sie stimmt '
        + 'nicht mit der konfigurierten öffentlichen Adresse überein.'
      : 'The collaboration server rejected this page address. It does not match the '
        + 'configured public address of the server.'
  }
  if (connectionStatus === 'access_revoked') {
    return locale === 'de'
      ? 'Der Zugriff auf dieses Kollaborationsdokument wurde entzogen.'
      : 'Access to this collaboration document was revoked.'
  }
  return locale === 'de'
    ? 'Die Kollaboration ist derzeit nicht schreibbereit.'
    : 'Collaboration is not currently writable.'
}
