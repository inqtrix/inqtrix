import { describe, expect, it } from 'vitest'

import type { AdminSystemRuntime, AdminUser } from '@/api/inqtrixClient'
import {
  activeAdminCount,
  auditDateInputValue,
  auditEpochFromDateInput,
  canDisable,
  canSetRole,
  deriveFeatureRows,
  deriveSystemFeatureRows,
  isLastActiveAdmin,
  isSelf,
  patRevealReducer,
  sortUsers,
} from './adminModel'

function user(partial: Partial<AdminUser> & { id: string }): AdminUser {
  return {
    disabled: false,
    display_name: partial.id,
    email: `${partial.id}@example.com`,
    instance_role: 'user',
    last_login_at: null,
    erasure_operation_id: null,
    ...partial,
  }
}

const owner = user({ id: 'owner', instance_role: 'admin' })
const admin2 = user({ id: 'admin2', instance_role: 'admin' })
const bob = user({ id: 'bob' })
const disabledAdmin = user({
  id: 'old',
  instance_role: 'admin',
  disabled: true,
})

describe('isSelf / activeAdminCount / isLastActiveAdmin', () => {
  it('detects the caller and counts only active admins', () => {
    expect(isSelf(owner, 'owner')).toBe(true)
    expect(isSelf(owner, 'bob')).toBe(false)
    expect(isSelf(owner, null)).toBe(false)
    expect(activeAdminCount([owner, admin2, bob, disabledAdmin])).toBe(2)
    expect(isLastActiveAdmin([owner, bob, disabledAdmin], owner)).toBe(true)
    expect(isLastActiveAdmin([owner, admin2], owner)).toBe(false)
  })

  it('does not count an admin whose account is being erased (like the server)', () => {
    const leaving = user({
      id: 'leaving',
      instance_role: 'admin',
      erasure_operation_id: 'del_1',
    })
    expect(activeAdminCount([owner, leaving])).toBe(1)
    expect(isLastActiveAdmin([owner, leaving], owner)).toBe(true)
  })
})

describe('canSetRole', () => {
  it('allows no-op and promotion, locks self-demote and last-admin demote', () => {
    // No-op (same role) is always fine.
    expect(canSetRole([owner], owner, 'owner', 'admin')).toEqual({ allowed: true })
    // Promote a plain user.
    expect(canSetRole([owner, bob], bob, 'owner', 'admin')).toEqual({
      allowed: true,
    })
    // Self-demote is locked even if another admin exists.
    expect(canSetRole([owner, admin2], owner, 'owner', 'user')).toEqual({
      allowed: false,
      reason: 'self',
    })
    // Demoting the last active admin is locked.
    expect(canSetRole([owner, bob], owner, 'someone-else', 'user')).toEqual({
      allowed: false,
      reason: 'last_admin',
    })
    // Demoting a non-last admin (caller is a third admin) is allowed.
    expect(canSetRole([owner, admin2], admin2, 'owner', 'user')).toEqual({
      allowed: true,
    })
  })
})

describe('canDisable', () => {
  it('locks self and the last active admin, allows everyone else', () => {
    expect(canDisable([owner, admin2], owner, 'owner')).toEqual({
      allowed: false,
      reason: 'self',
    })
    expect(canDisable([owner, bob], owner, 'someone-else')).toEqual({
      allowed: false,
      reason: 'last_admin',
    })
    expect(canDisable([owner, admin2], admin2, 'owner')).toEqual({
      allowed: true,
    })
    expect(canDisable([owner, bob], bob, 'owner')).toEqual({ allowed: true })
  })
})

describe('sortUsers', () => {
  it('puts admins first, then orders by email', () => {
    expect(sortUsers([bob, admin2, owner]).map((u) => u.id)).toEqual([
      'admin2',
      'owner',
      'bob',
    ])
  })
})

/** Every backing service configured but not answering. */
function degradedRuntime(): AdminSystemRuntime {
  return {
    api: { openapi: true, chat_max_concurrent: 100, stream_reader_workers: 128 },
    files: {
      blob_storage: 's3',
      document_parser: 'markitdown',
      enabled: true,
      max_file_bytes: 100,
      object_store: 's3',
      object_store_available: false,
    },
    knowledge: {
      contextual_retrieval: false,
      default_top_k: 8,
      embedding_model: 'text-embedding-3-large',
      embedding_provider: 'azure',
      enabled: true,
      hybrid_retrieval: true,
      reranker: 'none',
      sparse: 'bm25_german',
      vector_store: 'qdrant',
      vector_store_available: false,
    },
    runs: {
      execution: 'worker_dispatch',
      queue: 'valkey',
      queue_available: false,
      admission_max_concurrent: 100,
      queue_max_size: 100,
      queue_consumers: null,
      queue_depth: null,
      store: 'postgres',
      worker_dispatch: true,
    },
    storage: { backend: 'postgres', durable: true },
    observability: {
      tracing: 'off',
      tracing_active: false,
      content_capture: false,
      sample_rate: 1,
      spool: false,
      retention_enforced: true,
      retention_days: null,
      ui_link_configured: false,
    },
  }
}

function runtimeWithoutKnowledge(documentParser: string): AdminSystemRuntime {
  const runtime = degradedRuntime()
  return {
    ...runtime,
    files: { ...runtime.files, document_parser: documentParser },
    knowledge: { ...runtime.knowledge, enabled: false },
  }
}

describe('deriveFeatureRows', () => {
  it('derives sorted rows from the open features map (never hardcoded)', () => {
    expect(deriveFeatureRows(undefined)).toEqual([])
    expect(
      deriveFeatureRows({ sharing: true, knowledge: false, files: true }),
    ).toEqual([
      { key: 'files', on: true },
      { key: 'knowledge', on: false },
      { key: 'sharing', on: true },
    ])
  })

  it('keeps the document parser row on while knowledge is off', () => {
    const runtime = runtimeWithoutKnowledge('markitdown')
    expect(deriveSystemFeatureRows({ document_parser: true, knowledge: false }, runtime)).toEqual([
      { key: 'document_parser', on: true },
      { key: 'knowledge', on: false },
    ])
    expect(
      deriveSystemFeatureRows({ document_parser: true }, runtimeWithoutKnowledge('none')),
    ).toEqual([{ key: 'document_parser', on: false }])
  })

  it('folds runtime availability into infrastructure-backed feature rows', () => {
    const runtime = degradedRuntime()

    expect(
      deriveSystemFeatureRows(
        {
          files: true,
          hybrid_retrieval: true,
          knowledge: true,
          project_persistence: true,
        },
        runtime,
      ),
    ).toEqual([
      { key: 'files', on: false },
      { key: 'hybrid_retrieval', on: false },
      { key: 'knowledge', on: false },
      { key: 'project_persistence', on: true },
    ])
  })
})

describe('patRevealReducer', () => {
  it('reveals the plaintext once and drops it on dismiss', () => {
    const revealed = patRevealReducer(
      { phase: 'idle' },
      { name: 'CI', token: 'inq_secret', tokenId: 't1', type: 'reveal' },
    )
    expect(revealed).toEqual({
      phase: 'revealed',
      name: 'CI',
      token: 'inq_secret',
      tokenId: 't1',
    })
    expect(patRevealReducer(revealed, { type: 'dismiss' })).toEqual({
      phase: 'idle',
    })
  })
})

describe('audit date range', () => {
  it('renders an epoch bound in the operator timezone', () => {
    // Local, not UTC: the bound was created from a locally picked day,
    // so rendering it back through UTC would shift the field.
    const epoch = auditEpochFromDateInput('2026-07-25')
    expect(auditDateInputValue(epoch)).toBe('2026-07-25')
    expect(auditDateInputValue(undefined)).toBe('')
  })

  it('treats the upper bound as the FOLLOWING midnight', () => {
    // The range is half-open, so picking a day as "to" must include
    // that whole day — otherwise the last day silently returns nothing.
    const from = auditEpochFromDateInput('2026-07-25')
    const to = auditEpochFromDateInput('2026-07-25', true)
    expect(from).toBeDefined()
    expect(to).toBeDefined()
    expect((to as number) - (from as number)).toBe(86400)
  })

  it('maps empty and malformed input to no bound', () => {
    expect(auditEpochFromDateInput('')).toBeUndefined()
    expect(auditEpochFromDateInput('not-a-date')).toBeUndefined()
  })

  it('round-trips a picked day', () => {
    const epoch = auditEpochFromDateInput('2026-01-09')
    expect(auditDateInputValue(epoch)).toBe('2026-01-09')
  })
})
