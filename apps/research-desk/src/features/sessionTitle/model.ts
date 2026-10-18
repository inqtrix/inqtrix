/** Title ownership is independent of conversation activity and list sorting. */
export type TitleSource = 'initial' | 'generated' | 'manual'
export type TitleKind = 'chat' | 'agent'
export type TitleWire = {
  title: string
  title_revision?: number
  title_source?: TitleSource
  title_preserved?: boolean
}
export type SessionTitleFields = {
  titleRevision?: number
  titleSource?: TitleSource
  /** Local write receipt guard; never serialized into portable projects. */
  titleEditId?: string
  titlePending?: boolean
  titleServerReady?: boolean
  titleConflict?: string
  /** Only an explicit first-message submit creates this transient intent. */
  titleRequest?: { id: string; question: string; language?: string; stack?: string | null }
  titleGenerationReason?: string
}
export type TitledRecord = SessionTitleFields & { title: string }

export function shouldRequestAgentTitle(session: { persistable?: boolean; runIds: readonly string[] } | undefined): boolean {
  return !session || session.persistable === false || session.runIds.length === 0
}

export function sessionTitleHint(record: TitledRecord, copy: { pending: string; unavailable: string; conflict: (title: string) => string }, renameLabel: string): string {
  const detail = record.titleConflict ? copy.conflict(record.titleConflict)
    : record.titleRequest ? copy.pending
      : record.titleSource === 'initial' && record.titleGenerationReason ? copy.unavailable : ''
  return [record.title, renameLabel, detail].filter(Boolean).join('\n')
}

export function initialSessionTitle(kind: TitleKind, question: string): string {
  return kind === 'agent'
    ? question.trim().slice(0, 80)
    : question.replace(/\s+/g, ' ').trim().slice(0, 72) || 'New conversation'
}

export function titleWire(record: TitledRecord): TitleWire {
  return { title: record.title, title_revision: record.titleRevision ?? 0, title_source: record.titleSource ?? 'manual' }
}

export function titleFromWire(wire: TitleWire): TitledRecord {
  return { title: wire.title, titleRevision: wire.title_revision ?? 0,
    titleSource: wire.title_source ?? 'manual', titleServerReady: true }
}

/** A server reply may acknowledge only the local edit it actually saved. */
export function reconcileTitle(local: TitledRecord, wire: TitleWire, submitted?: TitledRecord): TitledRecord {
  if (local.titlePending && (!submitted || submitted.titleEditId !== local.titleEditId)) {
    return { title: local.title, titleRevision: local.titleRevision, titleSource: local.titleSource,
      titleServerReady: true, titlePending: true }
  }
  if ((wire.title_revision ?? 0) < (local.titleRevision ?? 0)) {
    return { title: local.title, titleRevision: local.titleRevision, titleSource: local.titleSource,
      titleServerReady: true, titlePending: local.titlePending }
  }
  return { ...titleFromWire(wire), titlePending: false,
    titleConflict: local.titlePending && submitted?.titleSource === 'manual'
      && (wire.title !== submitted.title || (wire.title_source ?? 'manual') !== 'manual')
      ? submitted.title : local.titlePending ? undefined : local.titleConflict }
}
