import assert from 'node:assert/strict'
import { describe, test } from 'node:test'
import { setImmediate as waitForImmediate } from 'node:timers/promises'

import {
  LOAD_SOAK_COMMENT_THREADS,
  LOAD_SOAK_USAGE_LEDGER_SETTLE_MS,
  LOAD_SOAK_WEB_SEARCH_REQUEST_LIMIT,
  LoadSoakProductActivity,
  assertLoadSoakCommentListing,
  commentBatchCounts,
  commentCommand,
  executeLoadSoakAgentActivity,
  expectedLoadSoakWebSearchRequests,
  readSettledLoadSoakFeatureBudget,
  summarizeLoadSoakFeatureBudget,
} from './load-soak-activity.mjs'
import { runAndFinalizeLoadSoakEvidence } from './load-soak-finalization.mjs'

describe('load-soak mixed product activity', () => {
  test('distributes exactly 50 comment threads across six phases', () => {
    const counts = commentBatchCounts()
    assert.equal(counts.length, 6)
    assert.equal(counts.reduce((total, count) => total + count, 0), LOAD_SOAK_COMMENT_THREADS)
    assert.deepEqual(counts, [9, 9, 8, 8, 8, 8])
  })

  test('validates non-empty unique public comment identities', () => {
    const listing = {
      current_revision: 65,
      data: Array.from({ length: LOAD_SOAK_COMMENT_THREADS }, (_, index) => ({
        id: `thread-${String(index + 1).padStart(2, '0')}`,
        status: index < 5 ? 'resolved' : 'open',
      })),
    }

    assert.deepEqual(assertLoadSoakCommentListing(listing), {
      commentRevision: 65,
      threadCount: LOAD_SOAK_COMMENT_THREADS,
    })

    const duplicate = structuredClone(listing)
    duplicate.data.at(-1).id = duplicate.data[0].id
    assert.throws(
      () => assertLoadSoakCommentListing(duplicate),
      /duplicate thread identities/,
    )

    const missing = structuredClone(listing)
    delete missing.data.at(-1).id
    assert.throws(
      () => assertLoadSoakCommentListing(missing),
      /thread identity/,
    )
  })

  test('builds live and explicitly orphaned anchors without changing generation', () => {
    const document = { generation: 7 }
    const live = commentCommand(document, {
      body: 'Live',
      expectedRevision: 11,
      threadId: '9eb90586-d3ce-4b68-9a02-aa1c3ff20a80',
    })
    const orphaned = commentCommand(document, {
      body: 'Orphan',
      expectedRevision: 12,
      orphaned: true,
      threadId: 'b21fd69b-d160-46ba-bfe1-5d8023557392',
    })
    assert.equal(live.generation, 7)
    assert.equal(live.expected_revision, 11)
    assert.equal(live.anchor.selectedText, 'System')
    assert.equal(orphaned.anchor.relativeFrom, 'orphaned-relative-from')
    assert(orphaned.anchor.from > 900_000)
  })

  test('separates priceable USD from the bounded unpriced web-search remainder', () => {
    const summary = summarizeLoadSoakFeatureBudget([
      usageReport([
        usageRow({ costUsd: 0.11, model: 'gpt-5.4', operation: 'chat' }),
        usageRow({
          costComplete: false,
          inputTokens: 51_166,
          model: 'foundry-web:web-search-agent@4',
          operation: 'web_search',
          outputTokens: 4_600,
          requestCount: 6,
        }),
      ]),
      usageReport([
        usageRow({ costUsd: 0.0034, model: 'gpt-5.4-mini', operation: 'chat' }),
        usageRow({
          costComplete: false,
          inputTokens: 321,
          model: 'foundry-web:web-search-agent@4',
          operation: 'web_search',
        }),
      ]),
    ])

    assert.deepEqual(summary, {
      complete: false,
      unpricedInputTokens: 51_487,
      unpricedModels: ['foundry-web:web-search-agent@4'],
      unpricedOutputTokens: 4_600,
      usd: 0.1134,
      webSearchRequests: LOAD_SOAK_WEB_SEARCH_REQUEST_LIMIT,
    })
  })

  test('rejects unsupported unpriced operations and incomplete usage accounting', () => {
    assert.throws(
      () => summarizeLoadSoakFeatureBudget([usageReport([
        usageRow({
          costComplete: false,
          model: 'unknown-embedding-model',
          operation: 'embeddings',
        }),
      ])]),
      /unsupported unpriced operation embeddings/,
    )

    const inconsistent = usageReport([
      usageRow({
        costComplete: false,
        model: 'foundry-web:web-search-agent@4',
        operation: 'web_search',
      }),
    ])
    inconsistent.total.is_complete = true
    assert.throws(
      () => summarizeLoadSoakFeatureBudget([inconsistent]),
      /completeness disagrees/,
    )

    const wrongAxes = usageReport([])
    wrongAxes.group_by = ['feature']
    assert.throws(
      () => summarizeLoadSoakFeatureBudget([wrongAxes]),
      /grouped by model and operation/,
    )
  })

  test('pins the cumulative web-search budget for every network phase', () => {
    assert.deepEqual(
      [
        'normal',
        'latency-100ms',
        'latency-300ms',
        'bandwidth-2mbit',
        'packet-loss-1pct',
        'normalized',
      ].map(expectedLoadSoakWebSearchRequests),
      [0, 0, 0, 6, 7, LOAD_SOAK_WEB_SEARCH_REQUEST_LIMIT],
    )
    assert.throws(
      () => expectedLoadSoakWebSearchRequests('unknown'),
      /unknown load-soak network phase/i,
    )
  })

  test('reads the feature budget only after one complete usage-ledger flush window', async () => {
    const events = []
    const budget = { usd: 0.1, webSearchRequests: 1 }
    const result = await readSettledLoadSoakFeatureBudget(
      async () => {
        events.push('read')
        return budget
      },
      async (milliseconds) => {
        events.push(`settle:${milliseconds}`)
      },
    )
    assert.equal(LOAD_SOAK_USAGE_LEDGER_SETTLE_MS, 6_000)
    assert.deepEqual(events, ['settle:6000', 'read'])
    assert.equal(result, budget)
  })

  test('does not read a budget when the usage-ledger barrier fails', async () => {
    let reads = 0
    await assert.rejects(
      () => readSettledLoadSoakFeatureBudget(
        async () => {
          reads += 1
          return {}
        },
        async () => {
          throw new Error('usage settle failed')
        },
      ),
      /usage settle failed/,
    )
    assert.equal(reads, 0)
  })

  test('observes a scheduled phase rejection immediately but keeps settle strict', async () => {
    const actor = {
      csrf: 'synthetic-csrf',
      email: 'synthetic@example.invalid',
      label: 'synthetic actor',
      user: { id: 'synthetic-user' },
      workspaceId: 'synthetic-workspace',
    }
    const activity = new LoadSoakProductActivity({
      commenters: Array.from({ length: 5 }, () => actor),
      document: { generation: 1, id: 'synthetic-document' },
      featureActors: Array.from({ length: 5 }, () => actor),
      lifecycle: { register: async () => ({ id: 'synthetic-handle' }) },
      moderator: actor,
      readers: Array.from({ length: 10 }, () => actor),
      runId: 'inqv-soak-activity-rejection-01',
      writers: Array.from({ length: 5 }, () => actor),
    })
    const unhandled = []
    const recordUnhandled = (reason) => unhandled.push(reason)
    process.on('unhandledRejection', recordUnhandled)
    try {
      const observed = activity.onNetworkPhase('normal')
      assert.equal(typeof observed?.then, 'function')
      await observed
      await waitForImmediate()
      assert.deepEqual(unhandled, [])
      await assert.rejects(() => activity.settle(), /request/)
    } finally {
      process.off('unhandledRejection', recordUnhandled)
    }
  })

  test('creates and registers the exact agent session before submitting its run', async () => {
    const actor = syntheticActor()
    const events = []
    const requests = []
    const lifecycle = {
      async register(resource) {
        events.push(`register:${resource.kind}:${resource.id}`)
        return { id: `handle:${resource.kind}:${resource.id}` }
      },
    }
    let expectedSessionId = null
    const fetchJson = async (_actor, method, path, options = {}) => {
      events.push(`request:${method}:${path}`)
      requests.push({ method, options, path })
      if (method === 'PUT') {
        expectedSessionId = path.split('/').at(-1)
        return {
          ...options.data,
          id: expectedSessionId,
          lifecycle_status: 'active',
        }
      }
      if (method === 'POST') {
        return {
          run_id: 'run_agent_session_contract',
          session_id: options.data.session_id,
          status: 'queued',
        }
      }
      if (method === 'GET') {
        return {
          created_at: 1_700_000_000,
          group_id: null,
          id: expectedSessionId,
          items_json: '[]',
          title: `${actor.runId} Agent-Soak`,
          updated_at: 1_700_000_000,
        }
      }
      throw new Error(`Unexpected request ${method} ${path}`)
    }

    const result = await executeLoadSoakAgentActivity({
      actor,
      fetchJson,
      lifecycle,
      nowSeconds: 1_700_000_000,
      runId: actor.runId,
      waitForRun: async (runId) => {
        events.push(`wait:${runId}`)
        return {
          run_id: runId,
          session_id: expectedSessionId,
          status: 'completed',
        }
      },
    })

    assert.match(result.sessionId, /^agent-session-[0-9a-f-]{36}$/)
    assert.equal(result.runId, 'run_agent_session_contract')
    assert.deepEqual(
      events.map((event) => event.split(':').slice(0, 2).join(':')),
      [
        'request:PUT',
        'register:agent_session',
        'request:POST',
        'register:agent_run',
        'request:GET',
        'wait:run_agent_session_contract',
      ],
    )
    assert.deepEqual(
      requests.map(({ method, path }) => [method, path]),
      [
        ['PUT', `/v1/agent-sessions/${result.sessionId}`],
        ['POST', '/v1/runs'],
        ['GET', `/v1/agent-sessions/${result.sessionId}`],
      ],
    )
    assert.equal(
      requests.some(({ method, path }) => method === 'GET' && path === '/v1/agent-sessions'),
      false,
    )
    assert.deepEqual(requests[0].options.data, {
      created_at: 1_700_000_000,
      group_id: null,
      items_json: '[]',
      title: `${actor.runId} Agent-Soak`,
      updated_at: 1_700_000_000,
    })
    assert.equal(requests[1].options.data.session_id, result.sessionId)
  })

  test('keeps an agent run bound to the requested session identity', async () => {
    const actor = syntheticActor()
    let sessionId = null
    await assert.rejects(
      () => executeLoadSoakAgentActivity({
        actor,
        fetchJson: async (_actor, method, path, options = {}) => {
          if (method === 'PUT') {
            sessionId = path.split('/').at(-1)
            return { ...options.data, id: sessionId }
          }
          if (method === 'POST') {
            return {
              run_id: 'run_wrong_session',
              session_id: 'agent-session-foreign',
            }
          }
          throw new Error(`Unexpected request ${method} ${path}`)
        },
        lifecycle: { register: async () => ({ id: 'handle' }) },
        nowSeconds: 1_700_000_000,
        runId: actor.runId,
        waitForRun: async () => ({ status: 'completed', session_id: sessionId }),
      }),
      /different agent session/i,
    )
  })
})

describe('load-soak evidence finalization', () => {
  test('retains collaboration and resource evidence when product activity fails', async () => {
    const observed = await exerciseFinalization({ activityFails: true })
    assert(observed.error instanceof AggregateError)
    assert.deepEqual(observed.events, ['load', 'activity', 'resources', 'evidence', 'scenarios'])
    assert.equal(observed.evidence.axes.collaboration, 'fulfilled')
    assert.equal(observed.evidence.axes.productActivity, 'rejected')
    assert.equal(observed.evidence.axes.resourceRecovery, 'fulfilled')
    assert.equal(observed.evidence.collaboration.writeSamples, 1_800)
    assert.equal(observed.evidence.productActivity, null)
    assert.equal(observed.evidence.resources.recovery.passed, true)
  })

  test('still finalizes product and resource axes when collaboration fails', async () => {
    const observed = await exerciseFinalization({ loadFails: true })
    assert(observed.error instanceof AggregateError)
    assert.deepEqual(observed.events, ['load', 'activity', 'resources', 'evidence', 'scenarios'])
    assert.equal(observed.evidence.axes.collaboration, 'rejected')
    assert.equal(observed.evidence.axes.productActivity, 'fulfilled')
    assert.equal(observed.evidence.axes.resourceRecovery, 'fulfilled')
    assert.equal(observed.evidence.collaboration, null)
    assert.equal(observed.scenarioInput.supplemental.featureActivityPassed, true)
  })

  test('records both failed execution axes before throwing once', async () => {
    const observed = await exerciseFinalization({ activityFails: true, loadFails: true })
    assert(observed.error instanceof AggregateError)
    assert.equal(observed.error.errors.length, 2)
    assert.deepEqual(observed.events, ['load', 'activity', 'resources', 'evidence', 'scenarios'])
    assert.deepEqual(observed.evidence.axes, {
      collaboration: 'rejected',
      productActivity: 'rejected',
      resourceRecovery: 'fulfilled',
    })
  })

  test('writes available load and activity evidence when resource capture fails', async () => {
    const observed = await exerciseFinalization({ resourcesFail: true })
    assert(observed.error instanceof AggregateError)
    assert.deepEqual(observed.events, ['load', 'activity', 'resources', 'evidence', 'scenarios'])
    assert.equal(observed.evidence.collaboration.passed, true)
    assert.equal(observed.evidence.productActivity.featureActivityPassed, true)
    assert.equal(observed.evidence.resources, null)
    assert.equal(observed.scenarioInput.supplemental.resourceRecoveryPassed, false)
  })

  test('attempts both persistence paths and remains failed when either write fails', async () => {
    const observed = await exerciseFinalization({ evidenceWriteFails: true })
    assert(observed.error instanceof AggregateError)
    assert.deepEqual(observed.events, ['load', 'activity', 'resources', 'evidence', 'scenarios'])

    const both = await exerciseFinalization({
      evidenceWriteFails: true,
      scenarioWriteFails: true,
    })
    assert(both.error instanceof AggregateError)
    assert.equal(both.error.errors.length, 2)
    assert.deepEqual(both.events, ['load', 'activity', 'resources', 'evidence', 'scenarios'])
  })

  test('returns only after all successful axes and persistence paths complete', async () => {
    const observed = await exerciseFinalization({})
    assert.equal(observed.error, null)
    assert.deepEqual(observed.events, ['load', 'activity', 'resources', 'evidence', 'scenarios'])
    assert.deepEqual(observed.evidence.axes, {
      collaboration: 'fulfilled',
      productActivity: 'fulfilled',
      resourceRecovery: 'fulfilled',
    })
    assert.deepEqual(observed.scenarioInput.supplemental, {
      commentsAndNavigationPassed: true,
      featureActivityPassed: true,
      identityMatrixPassed: true,
      resourceRecoveryPassed: true,
    })
  })
})

function syntheticActor() {
  const runId = 'inqv-load-soak-agent-contract-01'
  return {
    csrf: 'synthetic-csrf',
    email: 'synthetic@example.invalid',
    label: 'synthetic actor',
    runId,
    user: { id: 'synthetic-user' },
    workspaceId: 'synthetic-workspace',
  }
}

async function exerciseFinalization({
  activityFails = false,
  evidenceWriteFails = false,
  loadFails = false,
  resourcesFail = false,
  scenarioWriteFails = false,
}) {
  const events = []
  let evidence = null
  let scenarioInput = null
  let error = null
  try {
    evidence = await runAndFinalizeLoadSoakEvidence({
      captureResourceRecovery: async () => {
        events.push('resources')
        if (resourcesFail) throw new Error('resource failure')
        return {
          final: { api: { memoryBytes: 100 } },
          recovery: { passed: true },
          snapshots: [{ label: 'post-quiet' }],
        }
      },
      finishProductActivity: async () => {
        events.push('activity')
        if (activityFails) throw new Error('activity failure')
        return {
          commentsAndNavigationPassed: true,
          featureActivityPassed: true,
          threadCount: 50,
        }
      },
      runCollaboration: async () => {
        events.push('load')
        if (loadFails) throw new Error('load failure')
        return {
          connections: 25,
          gates: { phaseResultsPassed: true },
          passed: true,
          reconstruction: { passed: true },
          sessionRotation: { passed: true },
          writeSamples: 1_800,
        }
      },
      writeResourceEvidence: async (value) => {
        events.push('evidence')
        evidence = value
        if (evidenceWriteFails) throw new Error('evidence write failure')
      },
      writeScenarioEvidence: async (value) => {
        events.push('scenarios')
        scenarioInput = value
        if (scenarioWriteFails) throw new Error('scenario write failure')
      },
    })
  } catch (caught) {
    error = caught
  }
  return { error, events, evidence, scenarioInput }
}

function usageReport(rows) {
  const priced = rows.filter((row) => row.cost_complete)
  const unpriced = rows.filter((row) => !row.cost_complete)
  return {
    data: rows,
    group_by: ['model', 'operation'],
    object: 'usage_report',
    total: {
      cost_usd: rows.reduce((total, row) => total + row.cost_usd, 0),
      is_complete: unpriced.length === 0,
      priced_input_tokens: priced.reduce((total, row) => total + row.input_tokens, 0),
      priced_output_tokens: priced.reduce((total, row) => total + row.output_tokens, 0),
      unpriced_input_tokens: unpriced.reduce((total, row) => total + row.input_tokens, 0),
      unpriced_models: [...new Set(unpriced.map((row) => row.model))].sort(),
      unpriced_output_tokens: unpriced.reduce((total, row) => total + row.output_tokens, 0),
    },
  }
}

function usageRow({
  costComplete = true,
  costUsd = 0,
  inputTokens = 1,
  model,
  operation,
  outputTokens = 0,
  requestCount = 1,
}) {
  return {
    cost_complete: costComplete,
    cost_usd: costUsd,
    input_tokens: inputTokens,
    model,
    operation,
    output_tokens: outputTokens,
    request_count: requestCount,
  }
}
