import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { AgentSessionRecord } from './model'
import { DERIVED_AGENT_SESSION_UPDATED_AT } from './model'
import {
  agentSessionMetadataFromJson,
  agentSessionFingerprint,
  persistableAgentSessionsInOrder,
  pushAgentSessions,
  serverAgentSessionFingerprint,
  serverAgentSessionPayload,
} from './agentSessionSync'

// The autosave pass must never reach the session API on its own: a push goes
// through the caller's `save`, and a deletion only through the explicit
// deletion API. Any direct call shows up here.
const sessionApi = vi.hoisted(() => ({
  deleteAgentSession: vi.fn(),
  saveAgentSession: vi.fn(),
}))
vi.mock('@/api/inqtrixClient', () => sessionApi)

describe('agent session source policy serialization', () => {
  it('round-trips source availability through items_json', () => {
    const payload = serverAgentSessionPayload({
      id: 'session-1',
      title: 'Session',
      groupId: null,
      createdAt: '2026-07-10T10:00:00.000Z',
      updatedAt: '2026-07-10T10:01:00.000Z',
      runIds: [],
      sourcePolicy: { web: 'disabled', knowledge: 'available' },
    })
    expect(agentSessionMetadataFromJson(payload.items_json)).toEqual({
      // modelSelection rides the same blob; a session without a pick reports
      // null so the account preference can seed the composer.
      knowledgeSwitch: undefined,
      modelSelection: null,
      sourcePolicy: { web: 'disabled', knowledge: 'available' },
    })
  })

  it('round-trips the knowledge switch and makes it visible to the autosave diff', () => {
    const base = {
      id: 'session-1',
      title: 'Session',
      groupId: null,
      createdAt: '2026-07-10T10:00:00.000Z',
      updatedAt: '2026-07-10T10:01:00.000Z',
      runIds: [],
      sourcePolicy: { web: 'available', knowledge: 'available' },
    } satisfies AgentSessionRecord
    const on = serverAgentSessionPayload({ ...base, knowledgeSwitch: 'all' })
    expect(agentSessionMetadataFromJson(on.items_json).knowledgeSwitch).toBe('all')
    const off = serverAgentSessionPayload({ ...base, knowledgeSwitch: 'off' })
    expect(agentSessionMetadataFromJson(off.items_json).knowledgeSwitch).toBe('off')
    // A session that never asked stays absent (no glyph after a reload) and
    // an unknown wire value never becomes a switch.
    expect(agentSessionMetadataFromJson(serverAgentSessionPayload(base).items_json).knowledgeSwitch)
      .toBeUndefined()
    expect(agentSessionMetadataFromJson(JSON.stringify({ knowledge_switch: 'some' })).knowledgeSwitch)
      .toBeUndefined()
    // Fingerprint parity on both sides of the diff: a switched session must
    // be pushed, and the server row must read back as the same state.
    expect(agentSessionFingerprint({ ...base, knowledgeSwitch: 'all' }))
      .not.toBe(agentSessionFingerprint(base))
    expect(agentSessionFingerprint({ ...base, knowledgeSwitch: 'all' }))
      .not.toBe(agentSessionFingerprint({ ...base, knowledgeSwitch: 'off' }))
    const wire = {
      id: base.id,
      title: base.title,
      group_id: null,
      created_at: Date.parse(base.createdAt) / 1000,
      updated_at: Date.parse(base.updatedAt) / 1000,
      items_json: on.items_json,
    }
    expect(serverAgentSessionFingerprint(wire))
      .toBe(agentSessionFingerprint({ ...base, knowledgeSwitch: 'all' }))
  })

  it('loads older and malformed rows with safe available defaults', () => {
    expect(agentSessionMetadataFromJson('{}').sourcePolicy).toEqual({
      web: 'available',
      knowledge: 'available',
    })
    expect(agentSessionMetadataFromJson('{broken').sourcePolicy).toEqual({
      web: 'available',
      knowledge: 'available',
    })
  })

  it('fingerprints a server row before the reducer has hydrated it', () => {
    const wire = {
      created_at: 1_752_140_800,
      group_id: 'group-1',
      id: 'session-1',
      items_json: JSON.stringify({
        source_policy: { web: 'disabled', knowledge: 'available' },
      }),
      title: 'Session',
      updated_at: 1_752_140_860,
    }

    expect(serverAgentSessionFingerprint(wire)).toBe(
      agentSessionFingerprint({
        createdAt: new Date(wire.created_at * 1000).toISOString(),
        groupId: wire.group_id,
        id: wire.id,
        runIds: [],
        sourcePolicy: { web: 'disabled', knowledge: 'available' },
        title: wire.title,
        updatedAt: new Date(wire.updated_at * 1000).toISOString(),
      }),
    )
  })
})

describe('agent session model stickiness (wire)', () => {
  const base = {
    id: 'session-1',
    title: 'Session',
    groupId: null,
    createdAt: '2026-08-07T10:00:00.000Z',
    updatedAt: '2026-08-07T10:00:00.000Z',
    runIds: [],
    sourcePolicy: { web: 'available', knowledge: 'available' },
  } satisfies AgentSessionRecord

  it('round-trips the picked model through items_json', () => {
    const payload = serverAgentSessionPayload({
      ...base,
      modelSelection: { model: 'claude-opus-4-8', tier: null, effort: 'high' },
    })
    const parsed = agentSessionMetadataFromJson(payload.items_json)
    expect(parsed.modelSelection).toEqual({
      model: 'claude-opus-4-8',
      tier: null,
      effort: 'high',
    })
    expect(parsed.sourcePolicy.web).toBe('available')
  })

  it('reports no selection for a session that never had one', () => {
    const payload = serverAgentSessionPayload(base)
    expect(agentSessionMetadataFromJson(payload.items_json).modelSelection).toBeNull()
  })

  it('stays loadable for rows written before this feature', () => {
    const legacy = JSON.stringify({ source_policy: { web: 'disabled', knowledge: 'available' } })
    const parsed = agentSessionMetadataFromJson(legacy)
    expect(parsed.modelSelection).toBeNull()
    expect(parsed.sourcePolicy.web).toBe('disabled')
  })

  it('drops an unknown tier instead of pinning it', () => {
    const future = JSON.stringify({ model_selection: { model: null, tier: 'turbo', effort: null } })
    expect(agentSessionMetadataFromJson(future).modelSelection).toBeNull()
  })

  it('makes a changed model visible to the autosave diff', () => {
    // The fingerprint is the ONLY thing that decides whether a change is
    // pushed. A field missing from it syncs silently never.
    const a = agentSessionFingerprint(base)
    const b = agentSessionFingerprint({
      ...base,
      modelSelection: { model: null, tier: 'fast', effort: null },
    })
    const c = agentSessionFingerprint({
      ...base,
      modelSelection: { model: null, tier: 'high', effort: null },
    })
    expect(b).not.toBe(a)
    expect(c).not.toBe(b)
  })

  it('sees the same change from the server side of the diff', () => {
    const wire = (itemsJson: string) => ({
      id: 'session-1',
      title: 'Session',
      group_id: null,
      created_at: 1_700_000_000,
      updated_at: 1_700_000_000,
      items_json: itemsJson,
    })
    const withTier = serverAgentSessionFingerprint(wire(JSON.stringify({
      source_policy: { web: 'available', knowledge: 'available' },
      model_selection: { model: null, tier: 'fast', effort: null },
    })))
    const without = serverAgentSessionFingerprint(wire(JSON.stringify({
      source_policy: { web: 'available', knowledge: 'available' },
    })))
    expect(withTier).not.toBe(without)
  })
})

describe('persistable sessions (fabrication fence)', () => {
  const session = (overrides: Record<string, unknown> = {}) => ({
    id: 's1',
    title: 'Titel',
    groupId: null,
    createdAt: '2026-08-27T10:00:00.000Z',
    updatedAt: '2026-08-27T10:00:00.000Z',
    runIds: [],
    sourcePolicy: { knowledge: 'available' as const, web: 'available' as const },
    ...overrides,
  })

  it('never admits an untouched fabrication to the persistence API', () => {
    // A record fabricated from a run summary carries the epoch `updatedAt`
    // sentinel and represents nothing user-authored; pushing it would
    // resurrect its question-derived title over the stored one. Any user
    // mutation stamps a real `updatedAt` and re-admits the row.
    const sessions = {
      derived: session({
        id: 'derived',
        updatedAt: DERIVED_AGENT_SESSION_UPDATED_AT,
      }),
      real: session({ id: 'real' }),
      shared: session({ id: 'shared', persistable: false }),
    }
    const admitted = persistableAgentSessionsInOrder(sessions, [
      'derived',
      'real',
      'shared',
    ])
    expect(admitted.map((item) => item.id)).toEqual(['real'])
  })
})

describe('the autosave pass over agent sessions', () => {
  beforeEach(() => {
    sessionApi.deleteAgentSession.mockClear()
    sessionApi.saveAgentSession.mockClear()
  })

  const session = (overrides: Partial<AgentSessionRecord> & { id: string }): AgentSessionRecord => ({
    title: 'Titel',
    groupId: null,
    createdAt: '2026-10-08T09:00:00.000Z',
    updatedAt: '2026-10-08T09:00:00.000Z',
    runIds: [],
    sourcePolicy: { knowledge: 'available' as const, web: 'available' as const },
    ...overrides,
  })

  it('never deletes a session created while a push is awaited, and keeps its baseline', async () => {
    // createSession confirms a new row while this pass awaits a push and seeds
    // its baseline before the store holds the record; the pass ran on the
    // store as it was when it started. Deleting what that snapshot lacks
    // killed every such session ("Session not found" on the first message).
    const old = session({ id: 'agent-session-old', title: 'Alt' })
    const created = session({ id: 'agent-session-new', title: 'Neu' })
    const synced = new Map<string, string>()
    const saved: string[] = []
    await pushAgentSessions({
      sessions: { [old.id]: old },
      order: [old.id],
      synced,
      save: async (item) => {
        saved.push(item.id)
        synced.set(created.id, agentSessionFingerprint(created))
      },
    })
    expect(saved).toEqual([old.id])
    expect(synced.get(created.id)).toBe(agentSessionFingerprint(created))
    expect(sessionApi.deleteAgentSession).not.toHaveBeenCalled()
    expect(sessionApi.saveAgentSession).not.toHaveBeenCalled()
  })

  it('pushes only changed sessions, skips one under deletion and an untouched fabrication', async () => {
    const unchanged = session({ id: 'unchanged' })
    const changed = session({ id: 'changed', title: 'Neu benannt' })
    const deleting = session({
      id: 'deleting',
      title: 'Wird gelöscht',
      deletion: { error: null, operationId: 'op-1', stage: 'queued', status: 'deleting' },
    })
    const derived = session({ id: 'derived', updatedAt: DERIVED_AGENT_SESSION_UPDATED_AT })
    const synced = new Map<string, string>([
      [unchanged.id, agentSessionFingerprint(unchanged)],
      [changed.id, agentSessionFingerprint({ ...changed, title: 'Alter Titel' })],
    ])
    const saved: string[] = []
    await pushAgentSessions({
      sessions: { unchanged, changed, deleting, derived },
      order: ['unchanged', 'changed', 'deleting', 'derived'],
      synced,
      save: async (item) => { saved.push(item.id) },
    })
    expect(saved).toEqual(['changed'])
    expect(synced.get(changed.id)).toBe(agentSessionFingerprint(changed))
  })

  it('stops without a further save or baseline write once its project is gone', async () => {
    const first = session({ id: 'first', title: 'Eins' })
    const second = session({ id: 'second', title: 'Zwei' })
    const synced = new Map<string, string>()
    const saved: string[] = []
    let live = true
    await pushAgentSessions({
      inScope: () => live,
      order: ['first', 'second'],
      save: async (item) => {
        saved.push(item.id)
        live = false
      },
      sessions: { first, second },
      synced,
    })
    expect(saved).toEqual(['first'])
    expect(synced.size).toBe(0)
  })

  it('forgets the baseline of a derived view without a request', async () => {
    // A shared-run view never crosses the session API, even when a stale
    // baseline carries its id.
    const view = session({ id: 'shared-view', persistable: false })
    const synced = new Map<string, string>([[view.id, 'stale']])
    await pushAgentSessions({
      sessions: { [view.id]: view },
      order: [view.id],
      synced,
      save: async () => { throw new Error('a derived view is never saved') },
    })
    expect(synced.has(view.id)).toBe(false)
    expect(sessionApi.deleteAgentSession).not.toHaveBeenCalled()
  })
})
