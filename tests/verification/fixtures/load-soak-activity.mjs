import { randomUUID } from 'node:crypto'

import { SOAK_NETWORK_PHASES } from '../../load/collaboration-load-lib.mjs'
import { assertFixture, fetchActorJson } from './api.mjs'

export const LOAD_SOAK_COMMENT_THREADS = 50
export const LOAD_SOAK_FEATURE_COST_LIMIT_USD = 5
export const LOAD_SOAK_USAGE_LEDGER_SETTLE_MS = 6_000
export const LOAD_SOAK_WEB_SEARCH_REQUEST_LIMIT = 7

const COMMENT_COUNTS = Object.freeze([9, 9, 8, 8, 8, 8])
const WEB_SEARCH_REQUESTS_AFTER_PHASE = Object.freeze({
  'bandwidth-2mbit': 6,
  'latency-100ms': 0,
  'latency-300ms': 0,
  normal: 0,
  normalized: LOAD_SOAK_WEB_SEARCH_REQUEST_LIMIT,
  'packet-loss-1pct': LOAD_SOAK_WEB_SEARCH_REQUEST_LIMIT,
})
const TERMINAL_RUN_STATUSES = new Set([
  'cancelled',
  'completed',
  'expired',
  'failed',
])

export class LoadSoakProductActivity {
  #chain = Promise.resolve()
  #commentRevision = 0
  #commenters
  #document
  #featureActors
  #featureCostLimitUsd
  #knowledgeCollection = null
  #lifecycle
  #moderator
  #phaseIndex = 0
  #phaseResults = []
  #readers
  #runId
  #startedAtSeconds
  #threadIds = []
  #writers

  constructor({
    commenters,
    document,
    featureActors,
    featureCostLimitUsd = LOAD_SOAK_FEATURE_COST_LIMIT_USD,
    lifecycle,
    moderator,
    readers,
    runId,
    writers,
  }) {
    const expected = [
      ['commenters', commenters, 5],
      ['featureActors', featureActors, 5],
      ['readers', readers, 10],
      ['writers', writers, 5],
    ]
    for (const [label, actors, count] of expected) {
      if (!Array.isArray(actors) || actors.length !== count) {
        throw new Error(`Load-soak activity requires exactly ${count} ${label}.`)
      }
    }
    if (!document?.id || !Number.isSafeInteger(document.generation)) {
      throw new Error('Load-soak activity requires a collaboration document.')
    }
    if (!moderator?.user?.id || typeof lifecycle?.register !== 'function') {
      throw new Error('Load-soak activity requires an owner and lifecycle.')
    }
    if (!Number.isFinite(featureCostLimitUsd) || featureCostLimitUsd <= 0) {
      throw new Error('Load-soak feature cost limit must be positive.')
    }
    this.#commenters = commenters
    this.#document = document
    this.#featureActors = featureActors
    this.#featureCostLimitUsd = featureCostLimitUsd
    this.#lifecycle = lifecycle
    this.#moderator = moderator
    this.#readers = readers
    this.#runId = runId
    this.#startedAtSeconds = Date.now() / 1_000
    this.#writers = writers
  }

  async initialize() {
    const comments = await fetchActorJson(
      this.#moderator,
      'GET',
      `${commentPath(this.#document.id)}?since_revision=0&status=all&limit=100`,
    )
    assertFixture(
      Array.isArray(comments.data) && comments.data.length === 0,
      'Load-soak document must start without shared comments.',
    )
    assertFixture(
      Number.isSafeInteger(comments.current_revision),
      'Load-soak comment snapshot has no current revision.',
    )
    this.#commentRevision = comments.current_revision
  }

  onNetworkPhase(phaseId) {
    const expected = SOAK_NETWORK_PHASES[this.#phaseIndex]?.id
    if (phaseId !== expected) {
      throw new Error(
        `Load-soak product activity expected phase ${expected ?? 'none'}, received ${phaseId}.`,
      )
    }
    const phaseIndex = this.#phaseIndex
    this.#phaseIndex += 1
    this.#chain = this.#chain.then(async () => {
      const startedAt = performance.now()
      await Promise.all([
        this.#createCommentBatch(COMMENT_COUNTS[phaseIndex], phaseIndex),
        this.#navigateReaders(phaseId),
        this.#runFeatureActivity(phaseId),
      ])
      this.#phaseResults.push({
        durationMs: performance.now() - startedAt,
        id: phaseId,
        status: 'passed',
      })
    })
    return this.#chain.catch(() => undefined)
  }

  async finish() {
    await this.#chain
    assertFixture(
      this.#phaseIndex === SOAK_NETWORK_PHASES.length,
      'Load-soak product activity did not observe every network phase.',
    )
    const final = await fetchActorJson(
      this.#readers[0],
      'GET',
      `${commentPath(this.#document.id)}?since_revision=0&status=all&limit=100`,
    )
    const comments = assertLoadSoakCommentListing(final)
    const cost = await this.#featureCost()
    this.#assertFeatureBudget(cost, 'normalized')
    return {
      commentsAndNavigationPassed: true,
      commentRevision: comments.commentRevision,
      featureActivityPassed: true,
      featureCost: cost,
      phaseResults: this.#phaseResults,
      threadCount: comments.threadCount,
    }
  }

  async settle() {
    await this.#chain
  }

  async #createCommentBatch(count, phaseIndex) {
    for (let offset = 0; offset < count; offset += 1) {
      const ordinal = this.#threadIds.length + 1
      const actor = this.#commenters[(ordinal - 1) % this.#commenters.length]
      const threadId = randomUUID()
      const orphaned = ordinal % 10 === 0
      const created = await fetchActorJson(
        actor,
        'POST',
        commentPath(this.#document.id),
        {
          data: commentCommand(this.#document, {
            body: commentBody(this.#runId, ordinal, phaseIndex),
            expectedRevision: this.#commentRevision,
            mentions: ordinal % 4 === 0
              ? [this.#writers[ordinal % this.#writers.length].user.id]
              : [],
            orphaned,
            threadId,
          }),
        },
      )
      this.#commentRevision = requireRevision(created)
      this.#threadIds.push(threadId)
      if (ordinal % 5 === 0) {
        const replier = this.#commenters[ordinal % this.#commenters.length]
        const replied = await fetchActorJson(
          replier,
          'POST',
          `${commentPath(this.#document.id)}/${threadId}/replies`,
          {
            data: {
              ...mutationCommand(this.#document, this.#commentRevision),
              body_markdown: `${this.#runId} Antwort ${ordinal} – geprüft ✅`,
              mention_user_ids: [],
              message_id: randomUUID(),
            },
          },
        )
        this.#commentRevision = requireRevision(replied)
      }
      if (ordinal % 10 === 0) {
        const resolved = await fetchActorJson(
          this.#moderator,
          'PATCH',
          `${commentPath(this.#document.id)}/${threadId}`,
          {
            data: {
              ...mutationCommand(this.#document, this.#commentRevision),
              status: 'resolved',
            },
          },
        )
        this.#commentRevision = requireRevision(resolved)
      }
    }
  }

  async #navigateReaders(phaseId) {
    await Promise.all(this.#readers.map(async (actor) => {
      const [document, comments] = await Promise.all([
        fetchActorJson(actor, 'GET', `/v1/editor/documents/${this.#document.id}`),
        fetchActorJson(
          actor,
          'GET',
          `${commentPath(this.#document.id)}?since_revision=0&status=all&limit=50`,
        ),
      ])
      assertFixture(
        document.id === this.#document.id,
        `Reader navigation lost the load-soak document during ${phaseId}.`,
      )
      assertFixture(
        comments.object === 'list' && Array.isArray(comments.data),
        `Reader comment navigation failed during ${phaseId}.`,
      )
    }))
  }

  async #runFeatureActivity(phaseId) {
    if (phaseId === 'normal') {
      await this.#createKnowledgeFixture(this.#featureActors[0])
    } else if (phaseId === 'latency-100ms') {
      await this.#askKnowledge(this.#featureActors[0])
    } else if (phaseId === 'latency-300ms') {
      await this.#directChat(this.#featureActors[1], 'Latenzprofil')
    } else if (phaseId === 'bandwidth-2mbit') {
      await this.#research(this.#featureActors[2])
    } else if (phaseId === 'packet-loss-1pct') {
      await this.#agent(this.#featureActors[3])
    } else if (phaseId === 'normalized') {
      await this.#directChat(this.#featureActors[4], 'Normalisierung')
    }
    await this.#assertCostWithinBudget(phaseId)
  }

  async #createKnowledgeFixture(actor) {
    const name = `${this.#runId} Soak-Wissen`
    const collection = await fetchActorJson(
      actor,
      'POST',
      '/v1/knowledge/collections',
      { data: { name }, expected: [201] },
    )
    assertFixture(typeof collection.id === 'string', 'Knowledge collection has no ID.')
    await this.#lifecycle.register({
      credential: 'user',
      id: collection.id,
      kind: 'knowledge_collection',
      name,
      ownerEmail: actor.email,
    })
    this.#knowledgeCollection = collection
    const job = await fetchActorJson(
      actor,
      'POST',
      `/v1/knowledge/collections/${collection.id}/document-revisions`,
      {
        data: {
          metadata: {
            run_id: this.#runId,
            source: 'load-soak',
          },
          text: [
            'Inqtrix Soak-Fakt: Der Freigabecode lautet EICHE-27.',
            'Unicode-Prüfung: Grüße, Αθήνα, 東京 und 🚀.',
            'Diese synthetischen Daten dürfen nach dem Lauf gelöscht werden.',
          ].join('\n\n'),
          title: `${this.#runId} Wissensquelle`,
          workspace_id: actor.workspaceId,
        },
        expected: [202],
      },
    )
    assertFixture(typeof job.job_id === 'string', 'Knowledge indexing returned no job ID.')
    const terminal = await waitFor(
      async () => {
        const current = await fetchActorJson(
          actor,
          'GET',
          `/v1/knowledge/indexing-jobs/${job.job_id}`,
        )
        return ['cancelled', 'completed', 'failed'].includes(current.status)
          ? current
          : null
      },
      180_000,
      'knowledge indexing completion',
    )
    assertFixture(
      terminal.status === 'completed',
      `Knowledge indexing ended ${terminal.status}.`,
    )
  }

  async #askKnowledge(actor) {
    assertFixture(this.#knowledgeCollection?.id, 'Knowledge fixture is unavailable.')
    const response = await fetchActorJson(actor, 'POST', '/v1/chat/completions', {
      data: {
        knowledge_filters: {
          collection_ids: [this.#knowledgeCollection.id],
          profile: 'schnell',
          top_k: 4,
        },
        mode: 'knowledge',
        question: 'Welcher Freigabecode steht in der synthetischen Soak-Wissensquelle?',
        stream: false,
        workspace_id: actor.workspaceId,
      },
    })
    const answer = response.choices?.[0]?.message?.content ?? ''
    assertFixture(answer.includes('EICHE-27'), 'Knowledge answer omitted the known fact.')
  }

  async #directChat(actor, label) {
    const response = await fetchActorJson(actor, 'POST', '/v1/chat/completions', {
      data: {
        agent_overrides: { skip_search: true },
        mode: 'direct_llm',
        question: `${this.#runId} ${label}: Antworte ausschließlich mit „stabil“.`,
        stream: false,
        workspace_id: actor.workspaceId,
      },
    })
    assertFixture(
      typeof response.choices?.[0]?.message?.content === 'string'
        && response.choices[0].message.content.trim().length > 0,
      'Direct chat returned no answer.',
    )
  }

  async #research(actor) {
    const question = `${this.#runId} Kurzrecherche: Nenne die offizielle EU-Webseite zum AI Act.`
    const run = await fetchActorJson(actor, 'POST', '/v1/runs', {
      data: {
        agent_overrides: { report_profile: 'schnell' },
        mode: 'research',
        question,
        workspace_id: actor.workspaceId,
      },
      expected: [202],
    })
    assertFixture(typeof run.run_id === 'string', 'Research submission returned no run ID.')
    await this.#lifecycle.register({
      credential: 'user',
      id: run.run_id,
      kind: 'research_run',
      ownerEmail: actor.email,
      question,
    })
    const terminal = await this.#waitForRun(actor, run.run_id, 'research')
    assertFixture(terminal.status === 'completed', `Research run ended ${terminal.status}.`)
  }

  async #agent(actor) {
    await executeLoadSoakAgentActivity({
      actor,
      lifecycle: this.#lifecycle,
      runId: this.#runId,
      waitForRun: async (runId) => await this.#waitForRun(actor, runId, 'agent'),
    })
  }

  async #waitForRun(actor, runId, label) {
    return await waitFor(
      async () => {
        const current = await fetchActorJson(actor, 'GET', `/v1/runs/${runId}`)
        return TERMINAL_RUN_STATUSES.has(current.status) ? current : null
      },
      300_000,
      `${label} run completion`,
    )
  }

  async #assertCostWithinBudget(phaseId) {
    const cost = await readSettledLoadSoakFeatureBudget(
      async () => await this.#featureCost(),
    )
    this.#assertFeatureBudget(cost, phaseId)
  }

  #assertFeatureBudget(cost, phaseId) {
    const expectedWebSearchRequests = expectedLoadSoakWebSearchRequests(phaseId)
    assertFixture(
      cost.webSearchRequests === expectedWebSearchRequests,
      `Load-soak observed ${cost.webSearchRequests} web-search requests after ${phaseId}; expected exactly ${expectedWebSearchRequests}.`,
    )
    assertFixture(
      cost.usd <= this.#featureCostLimitUsd,
      `Load-soak priceable provider cost reached ${cost.usd.toFixed(4)} USD; further feature calls are refused.`,
    )
  }

  async #featureCost() {
    const reports = await Promise.all(this.#featureActors.map((actor) => (
      fetchActorJson(
        actor,
        'GET',
        `/v1/usage?since=${encodeURIComponent(this.#startedAtSeconds)}&group_by=model%2Coperation`,
      )
    )))
    return summarizeLoadSoakFeatureBudget(reports)
  }
}

export function assertLoadSoakCommentListing(final) {
  assertFixture(
    Array.isArray(final?.data) && final.data.length === LOAD_SOAK_COMMENT_THREADS,
    `Load-soak produced ${final?.data?.length ?? 0} comment threads instead of ${LOAD_SOAK_COMMENT_THREADS}.`,
  )
  assertFixture(
    final.data.every(
      (thread) => typeof thread?.id === 'string' && thread.id.trim().length > 0,
    ),
    'Load-soak comment listing contains a missing thread identity.',
  )
  assertFixture(
    new Set(final.data.map((thread) => thread.id)).size === LOAD_SOAK_COMMENT_THREADS,
    'Load-soak comment listing contains duplicate thread identities.',
  )
  assertFixture(
    final.data.filter((thread) => thread.status === 'resolved').length >= 5,
    'Load-soak comment workload did not retain resolved threads.',
  )
  return {
    commentRevision: final.current_revision,
    threadCount: final.data.length,
  }
}

export async function executeLoadSoakAgentActivity({
  actor,
  fetchJson = fetchActorJson,
  lifecycle,
  nowSeconds = Date.now() / 1_000,
  runId,
  waitForRun,
}) {
  if (
    !actor?.email
      || !actor?.workspaceId
      || typeof fetchJson !== 'function'
      || typeof lifecycle?.register !== 'function'
      || typeof runId !== 'string'
      || runId.length === 0
      || typeof waitForRun !== 'function'
      || !Number.isFinite(nowSeconds)
  ) {
    throw new Error('Load-soak agent activity requires a complete actor contract.')
  }
  const sessionId = `agent-session-${randomUUID()}`
  const sessionTitle = `${runId} Agent-Soak`
  const question = `${sessionTitle}: Finde die offizielle EU-Seite zum AI Act und antworte in einem Satz.`
  const session = await fetchJson(
    actor,
    'PUT',
    `/v1/agent-sessions/${sessionId}`,
    {
      data: {
        created_at: nowSeconds,
        group_id: null,
        items_json: '[]',
        title: sessionTitle,
        updated_at: nowSeconds,
      },
    },
  )
  await lifecycle.register({
    credential: 'user',
    id: sessionId,
    kind: 'agent_session',
    ownerEmail: actor.email,
    title: sessionTitle,
  })
  assertAgentSession(session, sessionId, sessionTitle, 'created')

  const run = await fetchJson(actor, 'POST', '/v1/runs', {
    data: {
      autonomy: 'autonomous',
      agent_overrides: { agent_tier: 'schnell' },
      question,
      response_form: 'chat',
      session_id: sessionId,
      workspace_id: actor.workspaceId,
    },
    expected: [202],
  })
  assertFixture(typeof run?.run_id === 'string', 'Agent submission returned no run ID.')
  await lifecycle.register({
    credential: 'user',
    id: run.run_id,
    kind: 'agent_run',
    ownerEmail: actor.email,
    sessionTitle,
  })
  assertFixture(
    run.session_id === sessionId,
    'Agent submission returned a different agent session identity.',
  )

  const storedSession = await fetchJson(
    actor,
    'GET',
    `/v1/agent-sessions/${sessionId}`,
  )
  assertAgentSession(storedSession, sessionId, sessionTitle, 'stored')
  const terminal = await waitForRun(run.run_id)
  assertFixture(terminal?.status === 'completed', `Agent run ended ${terminal?.status}.`)
  assertFixture(
    terminal.session_id === sessionId,
    'Completed agent run returned a different agent session identity.',
  )
  return { runId: run.run_id, sessionId }
}

function assertAgentSession(session, sessionId, title, stage) {
  assertFixture(
    session?.id === sessionId,
    `Load-soak ${stage} agent session returned a different identity.`,
  )
  assertFixture(
    session.title === title,
    `Load-soak ${stage} agent session returned a different title.`,
  )
  assertFixture(
    session.items_json === '[]',
    `Load-soak ${stage} agent session did not retain an empty item list.`,
  )
}

export function expectedLoadSoakWebSearchRequests(phaseId) {
  if (!Object.hasOwn(WEB_SEARCH_REQUESTS_AFTER_PHASE, phaseId)) {
    throw new Error(`Unknown load-soak network phase: ${String(phaseId)}.`)
  }
  return WEB_SEARCH_REQUESTS_AFTER_PHASE[phaseId]
}

export async function readSettledLoadSoakFeatureBudget(
  readBudget,
  wait = waitForUsageLedger,
) {
  if (typeof readBudget !== 'function' || typeof wait !== 'function') {
    throw new Error('Load-soak usage settlement requires read and wait callbacks.')
  }
  await wait(LOAD_SOAK_USAGE_LEDGER_SETTLE_MS)
  return await readBudget()
}

export function summarizeLoadSoakFeatureBudget(reports) {
  if (!Array.isArray(reports) || reports.length === 0) {
    throw new Error('Load-soak feature budget requires at least one usage report.')
  }
  let complete = true
  let unpricedInputTokens = 0
  const unpricedModels = new Set()
  let unpricedOutputTokens = 0
  let usd = 0
  let webSearchRequests = 0

  for (const report of reports) {
    assertUsageReportShape(report)
    let reportCost = 0
    let pricedInputTokens = 0
    let pricedOutputTokens = 0
    let reportUnpricedInputTokens = 0
    const reportUnpricedModels = new Set()
    let reportUnpricedOutputTokens = 0
    let reportComplete = true

    for (const row of report.data) {
      assertUsageRow(row)
      reportCost += row.cost_usd
      if (row.operation === 'web_search') {
        webSearchRequests += row.request_count
      }
      if (row.cost_complete) {
        pricedInputTokens += row.input_tokens
        pricedOutputTokens += row.output_tokens
        continue
      }
      if (row.operation !== 'web_search') {
        throw new Error(
          `Load-soak provider usage contains unsupported unpriced operation ${row.operation}.`,
        )
      }
      if (row.cost_usd !== 0) {
        throw new Error('Load-soak unpriced usage row contains a contradictory USD cost.')
      }
      reportComplete = false
      reportUnpricedInputTokens += row.input_tokens
      reportUnpricedModels.add(row.model)
      reportUnpricedOutputTokens += row.output_tokens
    }

    const total = report.total
    const expectedModels = [...reportUnpricedModels].sort()
    if (total.is_complete !== reportComplete) {
      throw new Error('Load-soak usage total completeness disagrees with row pricing.')
    }
    if (!sameNumber(total.cost_usd, reportCost)) {
      throw new Error('Load-soak usage total cost disagrees with its grouped rows.')
    }
    assertUsageTokenTotal(total.priced_input_tokens, pricedInputTokens, 'priced input')
    assertUsageTokenTotal(total.priced_output_tokens, pricedOutputTokens, 'priced output')
    assertUsageTokenTotal(
      total.unpriced_input_tokens,
      reportUnpricedInputTokens,
      'unpriced input',
    )
    assertUsageTokenTotal(
      total.unpriced_output_tokens,
      reportUnpricedOutputTokens,
      'unpriced output',
    )
    if (
      !Array.isArray(total.unpriced_models)
        || total.unpriced_models.some(
          (model) => typeof model !== 'string' || model.length === 0,
        )
        || JSON.stringify([...new Set(total.unpriced_models)].sort())
          !== JSON.stringify(expectedModels)
    ) {
      throw new Error('Load-soak usage total unpriced models disagree with its grouped rows.')
    }

    complete &&= reportComplete
    usd += reportCost
    unpricedInputTokens += reportUnpricedInputTokens
    unpricedOutputTokens += reportUnpricedOutputTokens
    for (const model of reportUnpricedModels) unpricedModels.add(model)
  }

  return {
    complete,
    unpricedInputTokens,
    unpricedModels: [...unpricedModels].sort(),
    unpricedOutputTokens,
    usd,
    webSearchRequests,
  }
}

function assertUsageReportShape(report) {
  if (
    !report
      || typeof report !== 'object'
      || report.object !== 'usage_report'
      || !Array.isArray(report.data)
      || !report.total
      || typeof report.total !== 'object'
  ) {
    throw new Error('Load-soak received an invalid usage report.')
  }
  if (
    !Array.isArray(report.group_by)
      || report.group_by.length !== 2
      || report.group_by[0] !== 'model'
      || report.group_by[1] !== 'operation'
  ) {
    throw new Error('Load-soak usage must be grouped by model and operation.')
  }
  if (
    typeof report.total.is_complete !== 'boolean'
      || !isNonNegativeFinite(report.total.cost_usd)
  ) {
    throw new Error('Load-soak usage report has an invalid total.')
  }
}

function assertUsageRow(row) {
  if (
    !row
      || typeof row !== 'object'
      || typeof row.model !== 'string'
      || row.model.length === 0
      || typeof row.operation !== 'string'
      || row.operation.length === 0
      || typeof row.cost_complete !== 'boolean'
      || !isNonNegativeFinite(row.cost_usd)
      || !isNonNegativeSafeInteger(row.input_tokens)
      || !isNonNegativeSafeInteger(row.output_tokens)
      || !Number.isSafeInteger(row.request_count)
      || row.request_count < 1
  ) {
    throw new Error('Load-soak received an invalid grouped usage row.')
  }
}

function assertUsageTokenTotal(actual, expected, label) {
  if (!isNonNegativeSafeInteger(actual) || actual !== expected) {
    throw new Error(`Load-soak usage total ${label} tokens disagree with its grouped rows.`)
  }
}

function isNonNegativeFinite(value) {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0
}

function isNonNegativeSafeInteger(value) {
  return Number.isSafeInteger(value) && value >= 0
}

function sameNumber(left, right) {
  return isNonNegativeFinite(left)
    && Math.abs(left - right) <= Number.EPSILON * Math.max(1, left, right) * 8
}

export function commentBatchCounts() {
  return [...COMMENT_COUNTS]
}

export function commentCommand(document, {
  body,
  expectedRevision,
  mentions = [],
  orphaned = false,
  threadId = randomUUID(),
}) {
  return {
    anchor: {
      from: orphaned ? 999_999 : 2,
      quoteAfter: '',
      quoteBefore: '',
      relativeFrom: orphaned ? 'orphaned-relative-from' : null,
      relativeTo: orphaned ? 'orphaned-relative-to' : null,
      relativeVersion: 'yjs-relative-position-base64-v1',
      selectedText: orphaned ? 'Nicht mehr vorhandener Text' : 'System',
      to: orphaned ? 1_000_010 : 8,
    },
    body_markdown: body,
    command_id: randomUUID(),
    expected_revision: expectedRevision,
    generation: document.generation,
    mention_user_ids: mentions,
    message_id: randomUUID(),
    quote: orphaned ? 'Nicht mehr vorhandener Text' : 'System',
    thread_id: threadId,
  }
}

function mutationCommand(document, expectedRevision) {
  return {
    command_id: randomUUID(),
    expected_revision: expectedRevision,
    generation: document.generation,
  }
}

function commentBody(runId, ordinal, phaseIndex) {
  const repeated = 'Langer synthetischer Prüfinhalt '.repeat((ordinal % 4) + 1).trim()
  return `${runId} Thread ${String(ordinal).padStart(2, '0')} · Phase ${phaseIndex + 1} · ${repeated} · Grüße 東京 🚀`
}

function commentPath(documentId) {
  return `/v1/editor/documents/${documentId}/collaboration/comments`
}

function requireRevision(value) {
  if (!Number.isSafeInteger(value?.revision) || value.revision < 1) {
    throw new Error('Shared-comment mutation returned no durable revision.')
  }
  return value.revision
}

async function waitFor(predicate, timeoutMs, label) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    const value = await predicate()
    if (value) return value
    await new Promise((resolvePromise) => setTimeout(resolvePromise, 500))
  }
  throw new Error(`Timed out waiting for ${label}.`)
}

async function waitForUsageLedger(milliseconds) {
  await new Promise((resolvePromise) => setTimeout(resolvePromise, milliseconds))
}
