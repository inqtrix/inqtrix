import type {
  InboxShare,
  OutgoingShare,
  ShareInvitee,
  ShareRecordInfo,
  SharingInbox,
  UserSearchResult,
} from './types'

/** The workspace owner shown in demo mode (matches the quota admin demo). */
export const DEMO_OWNER = {
  displayName: 'Olga Owner',
  email: 'olga@example.com',
  userId: '00000000-0000-4000-8000-000000000001',
}

/** The owned run that carries seeded shares + a badge in the demo. */
export const DEMO_SHARED_RUN_ID = 'run_52f64e5c818049cdae2d63feb25dc10b'
/** A run shared INTO the demo workspace (the recipient "shared with me" view). */
export const DEMO_SHARED_IN_RUN_ID = 'run_1e52ccb1a21f45f6a135052f321cd5f3'

// 2026-01-01T00:00:00Z — neutral fixed instant (anonymised demo data).
const DEMO_TS = 1_767_225_600

/** People the share-dialog typeahead resolves in demo (no backend). */
const demoPeople: UserSearchResult[] = [
  { display_name: 'Rita Recipient', email: 'rita@example.com', id: '00000000-0000-4000-8000-000000000002' },
  { display_name: 'Stefan Schulz', email: 'stefan@example.com', id: '00000000-0000-4000-8000-000000000003' },
  { display_name: 'Bianca Brandt', email: 'bianca@example.com', id: '00000000-0000-4000-8000-000000000004' },
  { display_name: 'Carlos Mendez', email: 'carlos@example.com', id: '00000000-0000-4000-8000-000000000005' },
]

export function searchDemoUsers(query: string): UserSearchResult[] {
  const q = query.trim().toLowerCase()
  if (q.length < 2) return []
  return demoPeople.filter(
    (user) =>
      (user.display_name ?? '').toLowerCase().includes(q) ||
      (user.email ?? '').toLowerCase().includes(q) ||
      user.id.toLowerCase().includes(q),
  )
}

// In-memory share store so grant/revoke in the dialog feel live offline.
// Keyed by `${resource_type}:${resource_id}`; seeded lazily on first read.
const store = new Map<string, ShareRecordInfo[]>()
let seeded = false
let counter = 1

const storeKey = (resourceType: string, resourceId: string) =>
  `${resourceType}:${resourceId}`

function makeRecord(
  resourceType: string,
  resourceId: string,
  person: UserSearchResult,
  permission: ShareInvitee['permission'],
  acceptedAt: number | null = null,
): ShareRecordInfo {
  counter += 1
  return {
    accepted_at: acceptedAt,
    created_at: DEMO_TS,
    display_name: person.display_name,
    email: person.email,
    granted_by_user_id: DEMO_OWNER.userId,
    id: `demo-share-${counter}`,
    permission,
    recipient_user_id: person.id,
    resource_id: resourceId,
    resource_type: resourceType,
    revision: 1,
  }
}

function ensureSeeded() {
  if (seeded) return
  seeded = true
  store.set(storeKey('run', DEMO_SHARED_RUN_ID), [
    makeRecord('run', DEMO_SHARED_RUN_ID, demoPeople[0], 'view', DEMO_TS),
    makeRecord('run', DEMO_SHARED_RUN_ID, demoPeople[1], 'edit'),
  ])
}

/** Title for the outgoing run shared in the demo (the `store` rows carry no
 * title, so the "Von mir geteilt" listing resolves it from here). Must match
 * the seeded run's question (the same run also appears in the run list), or the
 * panel and the list show one run under two titles. */
const DEMO_OUTGOING_RUN_TITLE =
  'Gibt es ein Tool für einen digitalen Kleiderschrank, was sind die aktuellen Entwicklungen auf diesem Gebiet?'

// The demo user's INBOX: shares others granted to them (pending + accepted).
// Seeded once per demo session; accept moves a row pending -> accepted, drop
// removes it — so all three panel sections are live offline.
type DemoInbox = { accepted: InboxShare[]; pending: InboxShare[] }
let inboxStore: DemoInbox | null = null

function seedInbox(): DemoInbox {
  return {
    pending: [
      {
        accepted_at: null,
        created_at: DEMO_TS,
        granted_by_display_name: 'Bianca Brandt',
        granted_by_user_id: '00000000-0000-4000-8000-000000000004',
        id: 'demo-inbox-1',
        permission: 'view',
        resource_id: 'run_demo_incoming_competitive',
        resource_title: 'Wettbewerbsanalyse Energiemarkt 2026',
        resource_type: 'run',
      },
      {
        accepted_at: null,
        created_at: DEMO_TS,
        granted_by_display_name: 'Stefan Schulz',
        granted_by_user_id: '00000000-0000-4000-8000-000000000003',
        id: 'demo-inbox-2',
        permission: 'edit',
        resource_id: 'kc_demo_incoming_market',
        resource_title: 'Marktstudien (Team)',
        resource_type: 'knowledge_collection',
      },
    ],
    accepted: [
      {
        accepted_at: DEMO_TS,
        created_at: DEMO_TS,
        granted_by_display_name: 'Bianca Brandt',
        granted_by_user_id: '00000000-0000-4000-8000-000000000004',
        id: 'demo-inbox-3',
        permission: 'view',
        resource_id: DEMO_SHARED_IN_RUN_ID,
        // Must match the seeded run's question (same run appears in the run
        // list's "Mit mir geteilt" divider) — no two titles for one run.
        resource_title:
          'Welche aktuellen Neuigkeiten gibt es zum Thema KI in den letzten 7 Tagen und welche Auswirkung gab es auf die Wirtschaft?',
        resource_type: 'run',
      },
    ],
  }
}

function ensureInbox(): DemoInbox {
  if (!inboxStore) inboxStore = seedInbox()
  return inboxStore
}

/** The demo user's incoming shares (pending + accepted). */
export function demoSharingInbox(): SharingInbox {
  const inbox = ensureInbox()
  return {
    accepted: inbox.accepted.map((item) => ({ ...item })),
    pending: inbox.pending.map((item) => ({ ...item })),
  }
}

/** Accept a pending incoming share in demo: pending -> accepted. */
export function acceptDemoShare(shareId: string) {
  const inbox = ensureInbox()
  const index = inbox.pending.findIndex((item) => item.id === shareId)
  if (index < 0) return
  const [item] = inbox.pending.splice(index, 1)
  inbox.accepted.push({ ...item, accepted_at: DEMO_TS })
}

/** Decline (pending) or leave (accepted) an incoming share in demo. */
export function dropDemoInboxShare(shareId: string) {
  const inbox = ensureInbox()
  inbox.pending = inbox.pending.filter((item) => item.id !== shareId)
  inbox.accepted = inbox.accepted.filter((item) => item.id !== shareId)
}

/** Resources the demo user has shared out, derived from the share `store` so
 * "Verwalten" opens the same recipient list the dialog shows. */
export function demoOutgoingShares(): OutgoingShare[] {
  ensureSeeded()
  const items: OutgoingShare[] = []
  for (const [key, records] of store) {
    if (records.length === 0) continue
    const separator = key.indexOf(':')
    const resourceType = key.slice(0, separator)
    const resourceId = key.slice(separator + 1)
    // Outgoing pending = how many of MY recipients have not accepted MY share —
    // independent of my own inbox (which is shares others made to me). The demo
    // store has no per-recipient accept state, so we seed one pending recipient
    // for the showcase run, bounded by the live recipient count so revoking via
    // the dialog can never leave pending > share_count.
    const seededPending = resourceId === DEMO_SHARED_RUN_ID ? 1 : 0
    items.push({
      pending_count: Math.min(seededPending, records.length),
      resource_id: resourceId,
      resource_title:
        resourceId === DEMO_SHARED_RUN_ID ? DEMO_OUTGOING_RUN_TITLE : resourceId,
      resource_type: resourceType,
      share_count: records.length,
    })
  }
  return items
}

/** Drop any in-session grant/revoke so re-entering demo starts fresh — the
 * rebuild-on-toggle invariant the rest of the demo state holds. Called when
 * the demo seed is (re)built. */
export function resetDemoShares() {
  store.clear()
  seeded = false
  counter = 1
  inboxStore = null
}

export function listDemoShares(
  resourceType: string,
  resourceId: string,
): ShareRecordInfo[] {
  ensureSeeded()
  return store.get(storeKey(resourceType, resourceId)) ?? []
}

export function grantDemoShares(
  resourceType: string,
  resourceId: string,
  invitees: ShareInvitee[],
) {
  ensureSeeded()
  const key = storeKey(resourceType, resourceId)
  const people = new Map(demoPeople.map((person) => [person.id, person]))
  const next = [...(store.get(key) ?? [])]
  for (const invitee of invitees) {
    const person = people.get(invitee.userId) ?? {
      display_name: null,
      email: null,
      id: invitee.userId,
    }
    const record = makeRecord(resourceType, resourceId, person, invitee.permission)
    const index = next.findIndex(
      (entry) => entry.recipient_user_id === invitee.userId,
    )
    if (index >= 0) next[index] = record
    else next.push(record)
  }
  store.set(key, next)
}

export function revokeDemoShare(shareId: string) {
  for (const [key, records] of store) {
    const next = records.filter((record) => record.id !== shareId)
    if (next.length !== records.length) store.set(key, next)
  }
}

export function updateDemoShare(
  shareId: string,
  permission: ShareRecordInfo['permission'],
  expectedRevision: number,
): ShareRecordInfo {
  for (const [key, records] of store) {
    const index = records.findIndex((record) => record.id === shareId)
    if (index < 0) continue
    const record = records[index]
    if (record.revision !== expectedRevision) {
      throw new Error('Die Freigabe wurde zwischenzeitlich geändert.')
    }
    const updated = {
      ...record,
      permission,
      revision: record.revision + 1,
    }
    const next = [...records]
    next[index] = updated
    store.set(key, next)
    return updated
  }
  throw new Error('Freigabe nicht gefunden.')
}
