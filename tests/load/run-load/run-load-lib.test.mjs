import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { readFileSync } from 'node:fs'
import { describe, test } from 'node:test'

import {
  HISTOGRAM_BUCKETS,
  LOOP_LAG_P99_LIMIT_MS,
  SSE_LAG_P95_LIMIT_MS,
  MAX_UNMATCHED_SHARE,
  NOT_MEASURED,
  POSTGRES_SAMPLE_SQL,
  SCENARIO,
  accountBelongsToPrefix,
  accountEmail,
  applyRunEvent,
  buildDirectChatRequest,
  buildMixTable,
  buildReport,
  buildRunRequest,
  cancelSpread,
  compareWithLimits,
  clarificationAnswerBody,
  createCountGauge,
  createHistogram,
  createMetrics,
  createPhaseGauges,
  createPhasedMetrics,
  createRng,
  createRunState,
  createSerialQueue,
  createSseParser,
  deliveryAccount,
  describeFailure,
  desiredActiveRoots,
  findForbiddenContent,
  formatSummaryLine,
  isLocalHostname,
  judgeBaseline,
  judgeConnectionBudget,
  judgeLoadApplied,
  judgeResidue,
  judgeScenarios,
  judgeWorkerStop,
  loopLagBetween,
  loopReadings,
  mapLimit,
  netPerUnit,
  networkAddresses,
  nextArrivalSeconds,
  normalizeBaseline,
  normalizeEndpoint,
  numberedQuestion,
  numericSettingsScript,
  parseCgroupStats,
  parseNumericSettings,
  parseSetCookie,
  phaseOverruns,
  parseValkeyCommands,
  percentile,
  pickWeighted,
  planNextRun,
  portOfUrl,
  projectContainers,
  publishedHostPorts,
  ratePerSecond,
  refreshPaths,
  requestTotals,
  residueSql,
  runDataSql,
  sessionIdFor,
  standInCoverage,
  statementsPerService,
  successRatio,
  thinkSeconds,
  unknownDegradedMessages,
  validateConfig,
  wiringCheckScript,
  worstLoopLag,
  reloadWaveWidth,
} from './run-load-lib.mjs'

const CONFIG = JSON.parse(
  readFileSync(new URL('./scenarios.json', import.meta.url), 'utf8'),
)
const UUID_A = '11111111-1111-4111-8111-111111111111'
const UUID_B = '22222222-2222-4222-8222-222222222222'

describe('reload wave', () => {
  test('a positive whole number asks for a wave of that width', () => {
    assert.equal(reloadWaveWidth('100'), 100)
  })

  test('anything else asks for none', () => {
    for (const value of [undefined, '', '0', '-3', '2.5', 'many']) {
      assert.equal(reloadWaveWidth(value), 0)
    }
  })
})

describe('connection budget', () => {
  const pool = (overrides = {}) => ({ max_overflow: 0, size: 15, timeouts: 0, ...overrides })
  const report = (overrides = {}) => ({
    connections_per_container_max: { 'api-1': { max: 15, service: 'api' } },
    plateau: { connections_new_per_minute: 12, seconds: 900 },
    probes: {
      final: { 'api-1': { checkpointer_pools: [{ pool_max: 4 }], pools: [pool()], service: 'api' } },
      idle_end: { 'api-1': { null_pool_connects: 0 } },
      plateau_end: { 'api-1': { null_pool_connects: 0 } },
    },
    ramp: { seconds: 300 },
    rest: { connections_new_per_minute: 10 },
    ...overrides,
  })
  const rule = (judgement, id) => judgement.rules.find((row) => row.id === id)
  const withFinal = (values) => {
    const base = report()
    return report({ probes: { ...base.probes, final: { 'api-1': { ...base.probes.final['api-1'], ...values } } } })
  }

  test('one pool, inside its limits and without new connections holds', () => {
    const judgement = judgeConnectionBudget(report())
    assert.equal(judgement.status, 'passed')
    assert.ok(judgement.rules.every((row) => row.status === 'passed'))
  })

  test('a second engine is red, even inside the connection limit', () => {
    const judgement = judgeConnectionBudget(withFinal({ pools: [pool(), pool({ size: 1 })] }))
    assert.equal(judgement.status, 'failed')
    assert.equal(rule(judgement, 'one_pool:api-1').status, 'failed')
  })

  test('a caller that ran out of connections is red', () => {
    const judgement = judgeConnectionBudget(withFinal({ pools: [pool({ timeouts: 1 })] }))
    assert.equal(rule(judgement, 'no_pool_timeout:api-1').status, 'failed')
  })

  test('the limit is the pool, its overflow, the checkpointer and one probe', () => {
    const held = (max) => report({
      connections_per_container_max: { 'api-1': { max, service: 'api' } },
    })
    assert.equal(rule(judgeConnectionBudget(held(20)), 'within_budget:api-1').status, 'passed')
    assert.equal(rule(judgeConnectionBudget(held(20)), 'within_budget:api-1').limit, 20)
    assert.equal(rule(judgeConnectionBudget(held(21)), 'within_budget:api-1').status, 'failed')
  })

  test('new connections under load beyond the samplers are red', () => {
    const at = (value) => report({ plateau: { connections_new_per_minute: value, seconds: 900 } })
    assert.equal(rule(judgeConnectionBudget(at(16)), 'new_connections_per_minute').status, 'passed')
    assert.equal(rule(judgeConnectionBudget(at(16.1)), 'new_connections_per_minute').status, 'failed')
  })

  test('in a short plateau the pools may still fill', () => {
    const judgement = judgeConnectionBudget(report({
      plateau: { connections_new_per_minute: 30, seconds: 60 },
    }))
    assert.equal(rule(judgement, 'new_connections_per_minute').status, 'passed')
    assert.equal(rule(judgement, 'new_connections_per_minute').limit, 30)
  })

  test('a caller that gave up while waiting shows without deciding', () => {
    const judgement = judgeConnectionBudget(withFinal({ pools: [pool({ abandoned: 2 })] }))
    assert.equal(rule(judgement, 'waits_abandoned:api-1').status, 'warning')
    assert.equal(judgement.status, 'passed')
    assert.deepEqual(judgement.warnings, ['waits_abandoned:api-1'])
  })

  test('a process that restarted counts from its new start', () => {
    const base = report()
    const judgement = judgeConnectionBudget(report({
      probes: {
        ...base.probes,
        idle_end: { 'api-1': { null_pool_connects: 40 } },
        plateau_end: { 'api-1': { null_pool_connects: 23 } },
      },
    }))
    assert.equal(rule(judgement, 'one_off_connections:api-1').current, 23)
    assert.equal(rule(judgement, 'one_off_connections:api-1').status, 'failed')
  })

  test('one-off connections beyond one a minute are red', () => {
    const base = report()
    const at = (value) => report({
      probes: { ...base.probes, plateau_end: { 'api-1': { null_pool_connects: value } } },
    })
    assert.equal(rule(judgeConnectionBudget(at(22)), 'one_off_connections:api-1').status, 'passed')
    assert.equal(rule(judgeConnectionBudget(at(23)), 'one_off_connections:api-1').status, 'failed')
  })

  test('what was not measured cannot show that a rule holds', () => {
    assert.equal(judgeConnectionBudget(report({ probes: {} })).status, 'failed')
    assert.equal(judgeConnectionBudget(withFinal({ pools: undefined })).status, 'failed')
    assert.equal(judgeConnectionBudget(report({ connections_per_container_max: {} })).status, 'failed')
    assert.equal(judgeConnectionBudget(report({ plateau: {} })).status, 'failed')
    assert.equal(judgeConnectionBudget(withFinal({ pools: [{ max_overflow: 0, size: 15 }] })).status, 'failed')
    const base = report()
    assert.equal(
      rule(judgeConnectionBudget(report({
        probes: { ...base.probes, plateau_end: { 'api-1': { status: NOT_MEASURED } } },
      })), 'one_off_connections:api-1').status,
      'failed',
    )
  })

  test('the verdict reaches the scenarios', () => {
    const verdict = (connectionBudget) => judgeScenarios({
      connectionBudget,
      coverage: { passed: true },
      provisioned: true,
      successRatioValue: 1,
      workloadRan: true,
    }).filter(({ id }) => id === SCENARIO.connectionBudget).map(({ status }) => status)
    assert.deepEqual(verdict(undefined), [])
    assert.deepEqual(verdict({ status: 'passed' }), ['passed'])
    assert.deepEqual(verdict({ status: 'failed' }), ['failed'])
  })
})

describe('scenarios.json', () => {
  test('passes its own validation', () => {
    assert.equal(validateConfig(CONFIG), CONFIG)
  })

  test('holds the stages of the capacity model', () => {
    const stage = ({
      decay_max_seconds: decay,
      idle_seconds: idle,
      plateau_seconds: plateau,
      ramp_seconds: ramp,
      rest_seconds: rest,
      roots,
      tabs,
    }) => ({ decay, idle, plateau, ramp, rest, roots, tabs })
    assert.deepEqual(stage(CONFIG.stages.smoke), {
      decay: 900, idle: 30, plateau: 60, ramp: 30, rest: 30, roots: 3, tabs: 6,
    })
    assert.deepEqual(stage(CONFIG.stages.l05), {
      decay: 600, idle: 120, plateau: 600, ramp: 150, rest: 60, roots: 25, tabs: 150,
    })
    assert.deepEqual(stage(CONFIG.stages.l1), {
      decay: 600, idle: 120, plateau: 900, ramp: 300, rest: 60, roots: 50, tabs: 300,
    })
    assert.deepEqual(stage(CONFIG.stages.l2), {
      decay: 600, idle: 120, plateau: 900, ramp: 300, rest: 60, roots: 100, tabs: 600,
    })
  })

  test('derives the mix from rates and shares', () => {
    const planned = Object.fromEntries(
      buildMixTable(CONFIG).map(({ id, weight }) => [id, Math.round(weight * 1000) / 1000]),
    )
    assert.deepEqual(planned, {
      agent_gruendlich_canvas: 0.06,
      agent_gruendlich_web: 0.14,
      agent_schnell_no_web: 0.4,
      agent_schnell_web: 0.4,
      agent_tief_web: 0.025,
      knowledge_schnell: 0.2,
      knowledge_standard: 0.15,
      research_compact: 0.35,
      research_deep: 0.025,
    })
    assert.equal(CONFIG.direct.rate_per_user_hour, 4)
  })

  test('a broken file is refused with what is wrong in it', () => {
    const broken = structuredClone(CONFIG)
    broken.scenarios.agent_schnell_web.share = 0.5
    assert.throws(() => validateConfig(broken), /shares of family schnell do not sum to 1/)
    const unknown = structuredClone(CONFIG)
    unknown.scenarios.research_deep.questions = ['nowhere']
    assert.throws(() => validateConfig(unknown), /unknown question: nowhere/)
    const wide = structuredClone(CONFIG)
    wide.provisioning.concurrency = 9
    assert.throws(() => validateConfig(wide), /concurrency must lie between 1 and 8/)
  })
})

describe('mix selection', () => {
  test('hits the weights at a fixed seed', () => {
    const table = buildMixTable(CONFIG)
    const rng = createRng(CONFIG.seed)
    const draws = 200_000
    const counts = new Map()
    for (let index = 0; index < draws; index += 1) {
      const { id } = pickWeighted(rng, table)
      counts.set(id, (counts.get(id) ?? 0) + 1)
    }
    const total = table.reduce((sum, entry) => sum + entry.weight, 0)
    assert.equal(counts.size, table.length)
    for (const entry of table) {
      const expected = entry.weight / total
      const observed = counts.get(entry.id) / draws
      assert.ok(
        Math.abs(observed - expected) < 0.005,
        `${entry.id}: observed ${observed}, expected ${expected}`,
      )
    }
  })

  test('one seed reproduces one sequence and another seed does not', () => {
    const table = buildMixTable(CONFIG)
    const sequence = (seed) => {
      const rng = createRng(seed)
      return Array.from({ length: 200 }, () => pickWeighted(rng, table).id)
    }
    assert.deepEqual(sequence(7), sequence(7))
    assert.notDeepEqual(sequence(7), sequence(8))
  })

  test('picks only a free open tab, and a collection scenario only where one exists', () => {
    const entry = (id, needsCollection) => ({ id, needs_collection: needsCollection, weight: 1 })
    const rng = createRng(3)
    const tabs = [
      { id: 'busy', open: true, busy: true, collectionId: 'kc' },
      { id: 'closed', open: false, busy: false, collectionId: 'kc' },
      { id: 'plain', open: true, busy: false, collectionId: null },
      { id: 'owner', open: true, busy: false, collectionId: 'kc' },
    ]
    for (let index = 0; index < 200; index += 1) {
      const plan = planNextRun({ rng, table: [entry('any', false)], tabs })
      assert.ok(['plain', 'owner'].includes(plan.tab.id))
      const collection = planNextRun({ rng, table: [entry('needs', true)], tabs })
      assert.equal(collection.tab.id, 'owner')
    }
  })

  test('draws again for a scenario nobody can serve, and says so', () => {
    const table = [
      { id: 'needs', needs_collection: true, weight: 1 },
      { id: 'any', weight: 1 },
    ]
    const tabs = [{ busy: false, collectionId: null, open: true }]
    const rng = createRng(11)
    let redraws = 0
    for (let index = 0; index < 200; index += 1) {
      const plan = planNextRun({ rng, table, tabs })
      assert.equal(plan.entry.id, 'any')
      redraws += plan.redraws
    }
    assert.ok(redraws > 0)
    assert.equal(planNextRun({ rng, table: [table[0]], tabs }), null)
  })

  test('the scenario sequence does not depend on who happens to be free', () => {
    const table = buildMixTable(CONFIG)
    const free = (count) => Array.from({ length: count }, () => ({
      busy: false,
      collectionId: 'kc',
      open: true,
    }))
    const sequence = (count) => {
      const rng = createRng(5)
      return Array.from(
        { length: 100 },
        () => planNextRun({ rng, table, tabs: free(count) }).entry.id,
      )
    }
    assert.deepEqual(sequence(1), sequence(50))
  })

  test('the active roots rise linearly to the target and stay there', () => {
    assert.equal(desiredActiveRoots(0, 300, 50), 0)
    assert.equal(desiredActiveRoots(150, 300, 50), 25)
    assert.equal(desiredActiveRoots(299, 300, 50), 50)
    assert.equal(desiredActiveRoots(300, 300, 50), 50)
    assert.equal(desiredActiveRoots(5_000, 300, 50), 50)
    assert.equal(desiredActiveRoots(75, 300, 100), 25)
  })

  test('about one gate in ten stays open for the late wait', () => {
    const rng = createRng(13)
    const think = { late_seconds: 120, late_share: 0.1, max_seconds: 20, min_seconds: 3 }
    const draws = Array.from({ length: 20_000 }, () => thinkSeconds(rng, think))
    const late = draws.filter((value) => value === 120).length / draws.length
    assert.ok(Math.abs(late - 0.1) < 0.01, String(late))
    for (const value of draws.filter((candidate) => candidate !== 120)) {
      assert.ok(value >= 3 && value <= 20)
    }
  })

  test('direct chats arrive at the rate of the open stream', () => {
    const rng = createRng(17)
    const draws = Array.from({ length: 50_000 }, () => nextArrivalSeconds(rng, 4, 300))
    const mean = draws.reduce((sum, value) => sum + value, 0) / draws.length
    assert.ok(Math.abs(mean - 3) < 0.1, String(mean))
    assert.equal(nextArrivalSeconds(rng, 4, 0), null)
  })

  test('mapLimit keeps the order and never runs more than its limit', async () => {
    let active = 0
    let peak = 0
    const results = await mapLimit([1, 2, 3, 4, 5, 6, 7], 3, async (value) => {
      active += 1
      peak = Math.max(peak, active)
      await new Promise((resolve) => setTimeout(resolve, 2))
      active -= 1
      return value * 2
    })
    assert.deepEqual(results, [2, 4, 6, 8, 10, 12, 14])
    assert.equal(peak, 3)
  })
})

describe('calls that must not overlap', () => {
  test('run one after another, in order, whatever the caller does at once', async () => {
    const inTurn = createSerialQueue()
    let active = 0
    let peak = 0
    const order = []
    const results = await Promise.all([3, 1, 2].map((value) => inTurn(async () => {
      active += 1
      peak = Math.max(peak, active)
      await new Promise((resolve) => setTimeout(resolve, value))
      order.push(value)
      active -= 1
      return value * 10
    })))
    assert.equal(peak, 1)
    assert.deepEqual(order, [3, 1, 2])
    assert.deepEqual(results, [30, 10, 20])
  })

  test('a failure stays with its caller and the next call still runs', async () => {
    const inTurn = createSerialQueue()
    const failing = inTurn(async () => {
      throw new Error('refused')
    })
    const next = inTurn(async () => 'ran')
    await assert.rejects(failing, /refused/)
    assert.equal(await next, 'ran')
  })
})

describe('requests as the surface sends them', () => {
  const ids = { collectionId: 'kc_1', sessionId: 'session-1', workspaceId: 'ws_1' }
  const scenario = (id) => ({ ...CONFIG.scenarios[id], id })

  test('an agent run without the web names every source as disabled', () => {
    assert.deepEqual(
      buildRunRequest({ ...ids, question: 'Q', scenario: scenario('agent_schnell_no_web') }),
      {
        agent_overrides: { agent_tier: 'schnell' },
        autonomy: 'balanced',
        mode: 'agent_kernel',
        question: 'Q',
        response_form: 'auto',
        session_id: 'session-1',
        source_policy: { knowledge: 'disabled', web: 'disabled' },
        workspace_id: 'ws_1',
      },
    )
  })

  test('an agent run with the web makes it available', () => {
    const body = buildRunRequest({ ...ids, question: 'Q', scenario: scenario('agent_gruendlich_web') })
    assert.deepEqual(body.agent_overrides, { agent_tier: 'gruendlich' })
    assert.deepEqual(body.source_policy, { knowledge: 'disabled', web: 'available' })
    assert.equal('knowledge_filters' in body, false)
  })

  test('an agent run on a collection pins it and opens the knowledge source', () => {
    const body = buildRunRequest({ ...ids, question: 'Q', scenario: scenario('agent_gruendlich_canvas') })
    assert.deepEqual(body.knowledge_filters, { collection_ids: ['kc_1'] })
    assert.deepEqual(body.source_policy, { knowledge: 'available', web: 'disabled' })
  })

  test('a knowledge run names its profile and the interface language', () => {
    assert.deepEqual(
      buildRunRequest({ ...ids, question: 'Q', scenario: scenario('knowledge_standard') }),
      {
        knowledge_filters: { collection_ids: ['kc_1'], profile: 'standard' },
        mode: 'knowledge',
        question: 'Q',
        session_id: 'session-1',
        ui_language: 'de',
        workspace_id: 'ws_1',
      },
    )
  })

  test('a research run carries its report profile and no session', () => {
    assert.deepEqual(
      buildRunRequest({ ...ids, question: 'Q', scenario: scenario('research_deep') }),
      {
        agent_overrides: { report_profile: 'deep' },
        mode: 'research',
        question: 'Q',
        workspace_id: 'ws_1',
      },
    )
  })

  test('a direct chat is a streamed plain completion', () => {
    assert.deepEqual(buildDirectChatRequest('Q'), {
      include_progress: false,
      messages: [{ content: 'Q', role: 'user' }],
      mode: 'direct_llm',
      model: 'research-agent',
      stream: true,
    })
  })

  test('questions differ by their number and sessions by their surface', () => {
    assert.equal(numberedQuestion('Warum?', 12), 'Warum? (Nr. 12)')
    assert.equal(sessionIdFor('agent_kernel', 'x'), 'agent-session-x')
    assert.equal(sessionIdFor('knowledge', 'x'), 'ks-x')
    assert.equal(sessionIdFor('research', 'x'), null)
  })

  test('a limit gate keeps the partial result and any other question gets the text', () => {
    assert.deepEqual(
      clarificationAnswerBody(
        { options: [{ id: 'extend' }, { id: 'partial' }, { id: 'cancel' }] },
        'Keine weiteren Vorgaben.',
      ),
      { option_id: 'partial' },
    )
    assert.deepEqual(
      clarificationAnswerBody({ options: [{ id: 'a' }], questions: [{ id: 'q' }] }, 'Text'),
      { answer: 'Text' },
    )
    assert.deepEqual(clarificationAnswerBody({}, 'Text'), { answer: 'Text' })
  })

  test('the user stream refreshes the lists of its scope and everything for the rest', () => {
    const config = CONFIG.user_events
    assert.deepEqual(refreshPaths({ all: false, scopes: new Set(['runs']) }, config), ['/v1/runs'])
    assert.deepEqual(
      refreshPaths({ all: false, scopes: new Set(['runs', 'indexing']) }, config),
      ['/v1/runs', '/v1/knowledge/collections'],
    )
    assert.deepEqual(refreshPaths({ all: false, scopes: new Set(['editor_documents']) }, config), [])
    assert.deepEqual(refreshPaths({ all: true, scopes: new Set() }, config), config.all)
    assert.deepEqual(refreshPaths({ all: false, scopes: new Set(['nobody_knows']) }, config), config.all)
  })
})

describe('following a run', () => {
  const event = (type, data = {}, extra = {}) => ({
    created_at: 1,
    data,
    run_id: 'run_1',
    sequence: 1,
    type,
    ...extra,
  })

  test('a wait for a person parks the run and its resumption clears it', () => {
    const state = createRunState('run_1')
    applyRunEvent(state, event('inqtrix.run.waiting', { status: 'waiting_for_approval' }))
    assert.equal(state.parked, 'waiting_for_approval')
    applyRunEvent(state, event('inqtrix.run.resumed'))
    assert.equal(state.parked, null)
    applyRunEvent(state, event('inqtrix.run.waiting', { status: 'waiting_for_input' }))
    assert.equal(state.parked, 'waiting_for_input')
    applyRunEvent(state, event('inqtrix.run.started'))
    assert.equal(state.parked, null)
  })

  test('a wait for children is not parked', () => {
    const state = createRunState('run_1')
    applyRunEvent(state, event('inqtrix.run.waiting', { status: 'waiting_for_children' }))
    assert.equal(state.parked, null)
  })

  test('the end of the run ends it, an end of something else does not', () => {
    const state = createRunState('run_1')
    applyRunEvent(state, event('inqtrix.run.completed', {}, { run_id: 'run_other' }))
    assert.equal(state.terminal, null)
    for (const type of ['inqtrix.run.completed', 'inqtrix.run.failed', 'inqtrix.run.cancelled']) {
      const ended = createRunState('run_1')
      applyRunEvent(ended, event(type))
      assert.equal(ended.terminal, type)
    }
  })

  test('the last sequence only moves forward', () => {
    const state = createRunState('run_1')
    applyRunEvent(state, event('inqtrix.progress.message', {}, { sequence: 9 }))
    applyRunEvent(state, event('inqtrix.progress.message', {}, { sequence: 4 }))
    assert.equal(state.lastSequence, 9)
  })

  test('a finished run with every number has lost and repeated nothing', () => {
    const state = createRunState('run_1')
    for (const sequence of [1, 2, 3]) {
      applyRunEvent(state, event('inqtrix.progress.message', {}, { sequence }))
    }
    applyRunEvent(state, event('inqtrix.run.completed', {}, { sequence: 4 }))
    assert.deepEqual(deliveryAccount(state), { duplicated: 0, lost: 0 })
  })

  test('a number that never came is lost, one that came twice is repeated', () => {
    const state = createRunState('run_1')
    for (const sequence of [1, 2, 2, 5]) {
      applyRunEvent(state, event('inqtrix.progress.message', {}, { sequence }))
    }
    applyRunEvent(state, event('inqtrix.run.failed', {}, { sequence: 6 }))
    assert.deepEqual(deliveryAccount(state), { duplicated: 1, lost: 2 })
  })

  test('a run left before its end has no total to hold the stream against', () => {
    const state = createRunState('run_1')
    applyRunEvent(state, event('inqtrix.progress.message', {}, { sequence: 7 }))
    assert.deepEqual(deliveryAccount(state), { duplicated: 0, lost: null })
  })

  test('a child gate opens once per transition, not with every progress event', () => {
    const state = createRunState('run_1')
    const progress = (status) => event(
      'inqtrix.agent.child.progress',
      { child_run_id: 'run_child', run_status: status },
    )
    assert.deepEqual(applyRunEvent(state, progress('running')), [])
    assert.deepEqual(applyRunEvent(state, progress('waiting_for_approval')), [
      { childRunId: 'run_child', status: 'waiting_for_approval' },
    ])
    assert.deepEqual(applyRunEvent(state, progress('waiting_for_approval')), [])
    assert.deepEqual(applyRunEvent(state, progress('running')), [])
    assert.deepEqual(applyRunEvent(state, progress('waiting_for_input')), [
      { childRunId: 'run_child', status: 'waiting_for_input' },
    ])
    assert.equal(state.parked, null)
  })
})

describe('percentiles', () => {
  test('the nearest rank of a kept list', () => {
    const values = Array.from({ length: 100 }, (_, index) => index + 1)
    assert.equal(percentile(values, 50), 50)
    assert.equal(percentile(values, 95), 95)
    assert.equal(percentile(values, 100), 100)
    assert.equal(percentile([7], 95), 7)
    assert.equal(percentile([], 95), null)
    assert.equal(percentile([3, 1, 2], 50), 2)
  })

  test('the histogram stays within its promised error over a wide range', () => {
    const histogram = createHistogram()
    const values = Array.from({ length: 20_000 }, (_, index) => (index + 1) * 0.5)
    for (const value of values) histogram.record(value)
    for (const rank of [50, 95, 99]) {
      const exact = percentile(values, rank)
      const estimate = histogram.percentile(rank)
      assert.ok(
        Math.abs(estimate - exact) / exact <= 0.04,
        `p${rank}: ${estimate} against ${exact}`,
      )
    }
    const { max_ms: maxMs, n, p50_ms: p50, p95_ms: p95 } = histogram.summary()
    assert.equal(n, 20_000)
    assert.equal(maxMs, 10_000)
    assert.ok(p50 < p95)
    assert.ok(p95 <= maxMs)
  })

  test('the histogram holds a single value and an empty one', () => {
    const one = createHistogram()
    one.record(123)
    assert.equal(one.percentile(50), 123)
    assert.equal(one.percentile(99), 123)
    assert.equal(createHistogram().percentile(50), null)
    assert.deepEqual(createHistogram().summary(), { n: 0 })
  })

  test('the histogram has a fixed size whatever it is fed', () => {
    const histogram = createHistogram()
    for (const value of [-5, 0, 1e-9, 1e15, Number.POSITIVE_INFINITY, Number.NaN]) histogram.record(value)
    assert.equal(histogram.n, 4)
    assert.ok(HISTOGRAM_BUCKETS <= 1024)
  })

  test('a count gauge has an exact maximum and median', () => {
    const gauge = createCountGauge()
    assert.equal(gauge.median(), null)
    for (const value of [1, 1, 2, 9, 3]) gauge.record(value)
    assert.equal(gauge.max, 9)
    assert.equal(gauge.median(), 2)
  })
})

describe('server-sent events', () => {
  const STREAM = 'event: ready\nid: 7\ndata: {"a":"ä€😀"}\n\n: keepalive\n\nevent: two\ndata: 1\ndata: 2\n\n'

  const framesOf = (chunks) => {
    const frames = []
    const parser = createSseParser((frame) => frames.push(frame))
    for (const chunk of chunks) parser.push(chunk)
    return { frames, parser }
  }

  const expected = [
    { comment: false, data: '{"a":"ä€😀"}', event: 'ready', id: '7' },
    { comment: true, data: null, event: null, id: null },
    { comment: false, data: '1\n2', event: 'two', id: null },
  ]

  test('reads frames, comments and multi-line data', () => {
    assert.deepEqual(framesOf([STREAM]).frames, expected)
  })

  test('survives a chunk boundary at every byte, inside a character too', () => {
    const bytes = Buffer.from(STREAM, 'utf8')
    for (let cut = 1; cut < bytes.length; cut += 1) {
      assert.deepEqual(
        framesOf([bytes.subarray(0, cut), bytes.subarray(cut)]).frames,
        expected,
        `cut at byte ${cut}`,
      )
    }
    assert.deepEqual(
      framesOf([...bytes].map((byte) => Uint8Array.of(byte))).frames,
      expected,
    )
  })

  test('accepts carriage returns, also split across chunks', () => {
    assert.deepEqual(
      framesOf(['event: a\r\ndata: 1\r', '\n\r', '\n']).frames,
      [{ comment: false, data: '1', event: 'a', id: null }],
    )
  })

  test('drops an unterminated last frame and says so', () => {
    const { frames, parser } = framesOf(['data: 1\n\ndata: 2\n'])
    assert.equal(frames.length, 1)
    assert.deepEqual(parser.end(), { discarded: true })
    const whole = framesOf(['data: 1\n\n'])
    assert.deepEqual(whole.parser.end(), { discarded: false })
  })

  test('an oversized frame is dropped and reported, the next one arrives', () => {
    const frames = []
    let overflows = 0
    const parser = createSseParser((frame) => frames.push(frame), {
      maxBufferedChars: 10,
      onOverflow: () => {
        overflows += 1
      },
    })
    parser.push(`data: ${'x'.repeat(50)}`)
    parser.push('tail\n\ndata: ok\n\n')
    assert.equal(overflows, 1)
    assert.deepEqual(frames.map((frame) => frame.data), ['ok'])
  })
})

describe('route names', () => {
  test('identifiers and the query leave the name', () => {
    for (const [method, path, expected] of [
      ['GET', '/v1/runs', 'GET /v1/runs'],
      ['POST', '/v1/runs', 'POST /v1/runs'],
      ['POST', '/v1/runs/import', 'POST /v1/runs/import'],
      ['GET', '/v1/runs/run_11d8b25baa224ca5/events?after=4', 'GET /v1/runs/<id>/events'],
      ['POST', '/v1/runs/run_1/approvals/appr_2', 'POST /v1/runs/<id>/approvals/<id>'],
      ['POST', '/v1/runs/run_1/clarifications/cl_2', 'POST /v1/runs/<id>/clarifications/<id>'],
      ['GET', '/v1/knowledge/collections', 'GET /v1/knowledge/collections'],
      [
        'POST',
        '/v1/knowledge/collections/kc_4c4a/document-revisions',
        'POST /v1/knowledge/collections/<id>/document-revisions',
      ],
      ['GET', '/v1/knowledge/indexing-jobs/job_1', 'GET /v1/knowledge/indexing-jobs/<id>'],
      ['GET', '/v1/deletion-operations/del_1', 'GET /v1/deletion-operations/<id>'],
      ['GET', `/v1/admin/users/${UUID_A}`, 'GET /v1/admin/users/<id>'],
      ['GET', '/v1/assets/sections', 'GET /v1/assets/sections'],
      ['PUT', '/v1/assets/default-sections', 'PUT /v1/assets/default-sections'],
      ['GET', `/v1/other/${UUID_B}/thing`, 'GET /v1/other/<id>/thing'],
    ]) {
      assert.equal(normalizeEndpoint(method, path), expected)
    }
  })

  test('the number of route names is bounded, the rest counts together', () => {
    const metrics = createMetrics()
    for (let index = 0; index < 500; index += 1) metrics.recordRequest(`GET /x${index}`, 1, 200)
    const { endpoints } = metrics.summary()
    assert.ok(Object.keys(endpoints).length <= 201)
    assert.ok(endpoints.other.n >= 299)
  })

  test('the metrics separate statuses, failures and phases', () => {
    const metrics = createPhasedMetrics(['idle', 'plateau'])
    metrics.setPhase('idle')
    metrics.recordRequest('GET /a', 5, 200)
    metrics.setPhase('plateau')
    metrics.recordRequest('GET /a', 9, 503)
    metrics.recordRequest('GET /a', 1, 429)
    metrics.recordRequestError('GET /a', 'ECONNRESET')
    metrics.count('runs_started')
    metrics.streamDrop('run_events', 'inactive')
    const plateau = metrics.phaseSummary('plateau')
    assert.deepEqual(plateau.endpoints['GET /a'].status_5xx, { 503: 1 })
    assert.deepEqual(plateau.endpoints['GET /a'].status_4xx, { 429: 1 })
    assert.deepEqual(plateau.endpoints['GET /a'].errors, { ECONNRESET: 1 })
    assert.deepEqual(plateau.stream_drops, { 'run_events:inactive': 1 })
    assert.equal(metrics.phaseSummary('idle').endpoints['GET /a'].n, 1)
    assert.equal(metrics.totalSummary().endpoints['GET /a'].n, 3)
    assert.equal(metrics.counter('runs_started'), 1)
    assert.equal(metrics.phaseCounter('idle', 'runs_started'), 0)
    assert.throws(() => metrics.setPhase('nowhere'), /Unknown run-load phase/)
  })

  test('a failure is reduced to its type, status and code', () => {
    assert.equal(
      describeFailure(new Error('rl-1 POST /v1/admin/users returned HTTP 409: {"email":"x@y.z"}')),
      'Error, HTTP 409',
    )
    const cause = Object.assign(new Error('secret url'), { code: 'ECONNRESET' })
    assert.equal(describeFailure(new TypeError('fetch failed', { cause })), 'TypeError, ECONNRESET')
    assert.equal(describeFailure('text'), 'Error')
  })
})

describe('accounts and sessions', () => {
  const PREFIX = 'inqv-rl-run-1-0123456789abcdef-u'

  test('only exactly this run\'s addresses qualify', () => {
    assert.equal(accountEmail(PREFIX, 12), `${PREFIX}12@example.invalid`)
    assert.equal(accountBelongsToPrefix(accountEmail(PREFIX, 12), PREFIX), true)
    for (const email of [
      'owner@example.invalid',
      `${PREFIX}@example.invalid`,
      `${PREFIX}12@example.org`,
      `${PREFIX}12x@example.invalid`,
      `${PREFIX}12@example.invalix`,
      `${PREFIX}12@evil@example.invalid`,
      `${PREFIX}12345@example.invalid`,
      `x${PREFIX}12@example.invalid`,
      'inqv-rl-run-1-fedcba9876543210-u12@example.invalid',
      undefined,
    ]) {
      assert.equal(accountBelongsToPrefix(email, PREFIX), false, String(email))
    }
  })

  test('a cookie keeps its attributes for the stored session', () => {
    assert.deepEqual(
      parseSetCookie(
        'inqtrix_session=abc=d; Path=/; HttpOnly; Secure; SameSite=strict; Max-Age=3600',
        'localhost',
      ),
      {
        domain: 'localhost',
        expires: -1,
        httpOnly: true,
        name: 'inqtrix_session',
        path: '/',
        sameSite: 'Strict',
        secure: true,
        value: 'abc=d',
      },
    )
    assert.equal(parseSetCookie('a=; Max-Age=0', 'localhost').deleted, true)
    assert.equal(parseSetCookie('no-pair', 'localhost'), null)
  })
})

describe('containers and counters from outside', () => {
  const row = (id, service, extra = {}) => ({
    Id: id.repeat(12),
    Labels: {
      'com.docker.compose.project': 'inqtrix-loadtest',
      'com.docker.compose.service': service,
    },
    Names: [`inqtrix-loadtest-${service}-1`],
    Ports: null,
    State: 'running',
    ...extra,
  })

  test('takes exactly the named project, replicas included', () => {
    const containers = projectContainers([
      row('a', 'api'),
      row('b', 'worker'),
      row('c', 'worker', { Names: ['inqtrix-loadtest-worker-2'] }),
      row('d', 'api', {
        Labels: { 'com.docker.compose.project': 'inqtrix', 'com.docker.compose.service': 'api' },
      }),
      row('e', 'postgres', { State: 'exited', Status: 'Exited (0)' }),
    ], 'inqtrix-loadtest')
    assert.deepEqual(containers.map(({ name, running, service }) => [name, running, service]), [
      ['inqtrix-loadtest-api-1', true, 'api'],
      ['inqtrix-loadtest-worker-1', true, 'worker'],
      ['inqtrix-loadtest-worker-2', true, 'worker'],
      ['inqtrix-loadtest-postgres-1', false, 'postgres'],
    ])
  })

  test('reads the published ports of both inventory forms', () => {
    assert.deepEqual([...publishedHostPorts({
      Ports: [{ container_port: 8080, host_port: 8081, range: 2 }],
    })], [8081, 8082])
    assert.deepEqual(
      [...publishedHostPorts({ Ports: '127.0.0.1:8081->8080/tcp, 0.0.0.0:9000->9000/tcp' })],
      [8081, 9000],
    )
    assert.deepEqual([...publishedHostPorts({ Ports: null })], [])
    assert.equal(portOfUrl('http://localhost:8081'), 8081)
    assert.equal(portOfUrl('http://localhost'), 80)
    assert.equal(portOfUrl('https://example.test'), 443)
  })

  test('the base URL must be this machine', () => {
    assert.equal(isLocalHostname('localhost'), true)
    assert.equal(isLocalHostname('127.0.0.1'), true)
    assert.equal(isLocalHostname('[::1]'), true)
    assert.equal(isLocalHostname('app.localhost'), true)
    assert.equal(isLocalHostname('10.211.55.4', ['10.211.55.4']), true)
    assert.equal(isLocalHostname('10.211.55.4', ['10.211.55.5']), false)
    assert.equal(isLocalHostname('example.test'), false)
  })

  test('the wiring check names every provider and prints only one digit', () => {
    const script = wiringCheckScript(CONFIG.stand_in)
    for (const variable of [
      ...CONFIG.stand_in.endpoint_variables,
      ...Object.keys(CONFIG.stand_in.provider_variables),
    ]) assert.match(script, new RegExp(`"\\$${variable}"`))
    assert.match(script, /case "\$AZURE_OPENAI_ENDPOINT" in http:\/\/mock-providers:\*\) ;; \*\) ok=0 ;; esac/)
    assert.equal(script.match(/echo/g).length, 1)
    assert.match(script, /echo "\$ok"$/)
  })

  const wired = {
    AZURE_OPENAI_ENDPOINT: 'http://mock-providers:9300/azure',
    INQTRIX_AGENT_MEMORY_PROVIDER: 'none',
    INQTRIX_EMBEDDING_AZURE_ENDPOINT: 'http://mock-providers:9300/azure-embeddings',
    INQTRIX_EMBEDDING_PROVIDER: 'azure',
    INQTRIX_LLM_PROVIDER: 'azure',
    INQTRIX_RERANKER_PROVIDER: 'none',
    INQTRIX_SEARCH_PROVIDER: 'perplexity',
    PERPLEXITY_BASE_URL: 'http://mock-providers:9300/perplexity',
  }
  const wiringVerdict = (environment) => spawnSync(
    'sh',
    ['-c', wiringCheckScript(CONFIG.stand_in)],
    { encoding: 'utf8', env: environment },
  ).stdout.trim()

  test('the wiring check run by a shell accepts exactly the wired stand-in', () => {
    assert.equal(wiringVerdict(wired), '1')
  })

  test('the wiring check run by a shell refuses every way to a real provider', () => {
    for (const [variable, value] of Object.entries({
      AZURE_OPENAI_ENDPOINT: 'https://real.openai.azure.com',
      INQTRIX_AGENT_MEMORY_PROVIDER: 'mem0',
      INQTRIX_EMBEDDING_AZURE_ENDPOINT: 'https://real.services.ai.azure.com',
      INQTRIX_EMBEDDING_PROVIDER: 'litellm',
      INQTRIX_LLM_PROVIDER: 'anthropic',
      INQTRIX_RERANKER_PROVIDER: 'cohere',
      INQTRIX_SEARCH_PROVIDER: 'azure_foundry',
      PERPLEXITY_BASE_URL: 'https://api.perplexity.ai',
    })) {
      assert.equal(wiringVerdict({ ...wired, [variable]: value }), '0', variable)
    }
    for (const variable of Object.keys(wired)) {
      const { [variable]: _removed, ...without } = wired
      assert.equal(wiringVerdict(without), '0', `${variable} missing`)
    }
  })

  test('a stand-in configuration without a list to check is refused', () => {
    const broken = (change) => {
      const copy = structuredClone(CONFIG)
      change(copy.stand_in)
      return copy
    }
    assert.throws(() => validateConfig(broken((standIn) => { standIn.endpoint_variables = [] })), /endpoint_variables is empty/)
    assert.throws(() => validateConfig(broken((standIn) => { standIn.provider_variables = {} })), /provider_variables is empty/)
    assert.throws(() => validateConfig(broken((standIn) => { standIn.wired_services = [] })), /wired_services is empty/)
  })

  test('the limits of a stack leave the container as digits or not at all', () => {
    const script = numericSettingsScript(['LIMIT_A', 'LIMIT_B', 'LIMIT_C'])
    const run = (env) => spawnSync('sh', ['-c', script], { encoding: 'utf8', env }).stdout
    const out = run({ LIMIT_A: '40', LIMIT_B: 'geheim' })
    assert.equal(out, '40\nunset\nunset\n')
    assert.deepEqual(parseNumericSettings(out, ['LIMIT_A', 'LIMIT_B', 'LIMIT_C']), {
      LIMIT_A: '40',
      LIMIT_B: 'unset',
      LIMIT_C: 'unset',
    })
    assert.deepEqual(parseNumericSettings('', ['LIMIT_A']), { LIMIT_A: NOT_MEASURED })
    assert.deepEqual(parseNumericSettings('geheim', ['LIMIT_A']), { LIMIT_A: NOT_MEASURED })
  })

  test('a configuration without the bounds of the applied load is refused', () => {
    const broken = structuredClone(CONFIG)
    delete broken.load_applied.max_rejected_share
    assert.throws(() => validateConfig(broken), /max_rejected_share/)
  })

  test('a known degraded message passes and an unknown one does not', () => {
    const known = CONFIG.stand_in.known_degraded
    assert.deepEqual(
      unknownDegradedMessages({ degraded: ['3 ambiguous routing signatures; those stay'] }, known),
      [],
    )
    assert.deepEqual(
      unknownDegradedMessages({ degraded: ['routing format changed; regenerate'] }, known),
      ['routing format changed; regenerate'],
    )
    assert.deepEqual(unknownDegradedMessages({}, known), [])
  })

  test('reads cgroup files, the broker counter and network addresses', () => {
    assert.deepEqual(parseCgroupStats('anon 1024\n37\nusage_usec 5000\n'), {
      anon_bytes: 1024,
      cpu_usec: 5000,
      threads: 37,
    })
    assert.equal(parseCgroupStats('grep: no such file\n'), null)
    assert.equal(
      parseValkeyCommands('# Stats\r\ntotal_connections_received:9\r\ntotal_commands_processed:4711\r\n'),
      4711,
    )
    assert.equal(parseValkeyCommands('NOAUTH'), null)
    assert.deepEqual(
      networkAddresses({ one: { GlobalIPv6Address: '', IPAddress: '10.89.0.5' } }),
      ['10.89.0.5'],
    )
  })

  test('the database reads select counters, never the content of a row', () => {
    for (const sql of [POSTGRES_SAMPLE_SQL, runDataSql([UUID_A, UUID_B])]) {
      assert.doesNotMatch(sql, /\bquestion\b|\bresult\b|\bsnapshot\b|\brequest_payload\b|\bquery\b/)
      assert.doesNotMatch(sql, /select \*/i)
    }
    assert.match(runDataSql([UUID_A]), /octet_length\(e\.data::text\)/)
    assert.equal(runDataSql([UUID_A]).match(/e\.data/g).length, 1)
  })

  test('a database read takes account identifiers and nothing else', () => {
    assert.match(residueSql('runs', 'created_by_user_id', [UUID_A]), new RegExp(UUID_A))
    assert.throws(() => residueSql('runs', 'id', ["x'; drop table runs; --"]), /invalid identifier/)
    assert.throws(() => runDataSql(['not-a-uuid']), /invalid identifier/)
  })

  test('phase gauges keep maxima, the slot median and what could not be read', () => {
    const gauges = createPhaseGauges()
    const owners = (address) => (
      address === '10.0.0.1'
        ? { name: 'stack-api-1', service: 'api' }
        : { name: 'stack-worker-1', service: 'worker' }
    )
    gauges.recordPostgres({
      active: 3,
      by_client: { '10.0.0.1': 10, '10.0.0.2': 4 },
      idle_in_tx: 1,
      lock_waits: [{ locktype: 'tuple', mode: 'ShareLock', n: 2 }],
      locks_waiting: 2,
      runs: { queued: 5, running: 8 },
      total: 14,
    }, owners)
    gauges.recordPostgres({
      active: 1,
      by_client: { '10.0.0.1': 12 },
      idle_in_tx: 0,
      lock_waits: [],
      locks_waiting: 0,
      runs: { running: 2 },
      total: 12,
    }, owners)
    gauges.recordPostgres({
      active: 1,
      by_client: {},
      idle_in_tx: 0,
      lock_waits: [{ locktype: 'tuple', mode: 'ShareLock', n: 1 }],
      locks_waiting: 1,
      runs: { running: 3 },
      total: 1,
    }, owners)
    gauges.recordContainer({ name: 'stack-api-1', service: 'api' }, { anon_bytes: 5, cpu_usec: 1_000_000, threads: 40 }, 100)
    gauges.recordContainer({ name: 'stack-api-1', service: 'api' }, { anon_bytes: 9, cpu_usec: 3_500_000, threads: 30 }, 110)
    gauges.recordContainer({ name: 'stack-qdrant-1', service: 'qdrant' }, null, 100)
    const summary = gauges.summary()
    assert.deepEqual(summary.slots.running_max, 8)
    assert.deepEqual(summary.slots.running_median, 3)
    assert.deepEqual(summary.slots.queued_max, 5)
    assert.deepEqual(summary.connections_per_container, {
      'stack-api-1': { max: 12, service: 'api' },
      'stack-worker-1': { max: 4, service: 'worker' },
    })
    assert.deepEqual(summary.lock_samples, [
      { locktype: 'tuple', max_waiting: 2, mode: 'ShareLock', samples: 2 },
    ])
    assert.deepEqual(summary.containers['stack-api-1'], {
      anon_bytes_growth: 4,
      anon_bytes_growth_second_half: 0,
      anon_bytes_max: 9,
      cpu_cores: 0.25,
      cpu_seconds: 2.5,
      service: 'api',
      threads_max: 40,
    })
    assert.deepEqual(summary.containers['stack-qdrant-1'], {
      anon_bytes_growth: NOT_MEASURED,
      anon_bytes_growth_second_half: NOT_MEASURED,
      anon_bytes_max: NOT_MEASURED,
      cpu_cores: NOT_MEASURED,
      cpu_seconds: NOT_MEASURED,
      service: 'qdrant',
      threads_max: NOT_MEASURED,
    })
    assert.equal(createPhaseGauges().summary().slots.running_max, NOT_MEASURED)
  })

  test('the connections of one container add up over its addresses', () => {
    const gauges = createPhaseGauges()
    gauges.recordPostgres({
      by_client: { '10.0.0.1': 10, '10.0.0.2': 4, local: 1 },
      runs: {},
      total: 15,
    }, (address) => (
      address === 'local'
        ? { name: 'local', service: 'local' }
        : { name: 'stack-api-1', service: 'api' }
    ))
    assert.deepEqual(gauges.summary().connections_per_container, {
      local: { max: 1, service: 'local' },
      'stack-api-1': { max: 14, service: 'api' },
    })
  })

  describe('the loop lag of a window', () => {
    const limits = [10, 50, 100]
    const reading = (...counts) => ({ counts, limits_ms: limits })

    test('is the difference of two readings, not everything since the start', () => {
      // Six hundred quiet samples before the window would hide a stalled window.
      const lag = loopLagBetween(
        { api: reading(600, 0, 0, 0) },
        { api: reading(610, 0, 90, 0) },
      )
      assert.equal(lag.api.n, 100)
      assert.equal(lag.api.p50_ms, 100)
      assert.equal(lag.api.p99_ms, 100)
      assert.equal(lag.api.share_over_limit, 0.9)
    })

    test('names the last limit for a lag beyond it', () => {
      const lag = loopLagBetween({ api: reading(0, 0, 0, 0) }, { api: reading(0, 0, 0, 5) })
      assert.equal(lag.api.p99_ms, 100)
      assert.equal(lag.api.share_over_limit, 1)
    })

    test('a process without a reading or without samples is left out', () => {
      assert.deepEqual(loopLagBetween({}, { api: reading(1, 0, 0, 0) }), {})
      assert.deepEqual(loopLagBetween({ api: reading(1, 0, 0, 0) }, { api: reading(1, 0, 0, 0) }), {})
      assert.deepEqual(loopLagBetween(undefined, undefined), {})
    })

    test('takes the worst process, and without any there is no number', () => {
      assert.equal(worstLoopLag({ one: { p99_ms: 12 }, two: { p99_ms: 31 } }), 31)
      assert.equal(worstLoopLag({}), null)
    })

    test('reads the histograms of the processes that have one', () => {
      assert.deepEqual(loopReadings({
        api: { service: 'api', values: { loop_lag: { counts: [1], limits_ms: [10], n: 1 } } },
        worker: { service: 'worker', values: { loop_lag: { counts: [] } } },
        gone: { service: 'api', values: null },
      }), { api: { counts: [1], limits_ms: [10] } })
    })
  })
})

describe('net transactions per unit of work', () => {
  test('takes the idle rate over the phase off what the phase committed', () => {
    assert.equal(netPerUnit({ delta: 1_000, idleRatePerSecond: 2, units: 10, seconds: 100 }), 80)
    assert.equal(netPerUnit({ delta: 1_000, idleRatePerSecond: 5, units: 10, seconds: 100 }), 50)
    assert.equal(netPerUnit({ delta: 1_000, idleRatePerSecond: 2, units: 4, seconds: 100 }), 200)
    assert.equal(netPerUnit({ delta: 1_000, idleRatePerSecond: 2, units: 10, seconds: 200 }), 60)
  })

  test('without a finished run or a reading it is no number', () => {
    assert.equal(netPerUnit({ delta: 1_000, idleRatePerSecond: 2, units: 0, seconds: 100 }), null)
    assert.equal(netPerUnit({ delta: Number.NaN, idleRatePerSecond: 2, units: 1, seconds: 100 }), null)
    assert.equal(netPerUnit({ delta: 1, idleRatePerSecond: null, units: 1, seconds: 100 }), null)
  })

  test('a rate needs both samples and a length', () => {
    assert.equal(ratePerSecond(10, 40, 10), 3)
    assert.equal(ratePerSecond(null, 40, 10), null)
    assert.equal(ratePerSecond(10, undefined, 10), null)
    assert.equal(ratePerSecond(10, 40, 0), null)
  })

  test('the success ratio is finished work of what was started', () => {
    assert.equal(successRatio({ completed: 9, started: 10 }), 0.9)
    assert.equal(successRatio({ completed: 0, started: 0 }), null)
  })
})

describe('stand-in coverage', () => {
  const stats = (total, unmatched = {}, unmatchedCalls = 0, extra = {}) => ({
    calls_per_operation: unmatchedCalls ? { unmatched: unmatchedCalls } : {},
    synthesised_replies: {},
    tool_calls_dropped: {},
    total_calls: total,
    unmatched_instructions: unmatched,
    ...extra,
  })

  test('holds at the limit and fails just above it', () => {
    const before = stats(0)
    const atLimit = standInCoverage(before, stats(1000, { abc: 20 }, 20))
    assert.equal(atLimit.unmatched_share, 0.02)
    assert.equal(atLimit.passed, true)
    const above = standInCoverage(before, stats(1000, { abc: 21 }, 21))
    assert.equal(above.unmatched_share, 0.021)
    assert.equal(above.passed, false)
    assert.equal(MAX_UNMATCHED_SHARE, 0.02)
  })

  test('counts only what happened since the run began', () => {
    const coverage = standInCoverage(
      stats(5000, { old: 900 }, 900),
      stats(6000, { old: 905, fresh: 5 }, 910),
    )
    assert.equal(coverage.total_calls, 1000)
    assert.equal(coverage.unmatched_calls, 10)
    assert.deepEqual(coverage.unmatched_instructions, { fresh: 5, old: 5 })
    assert.equal(coverage.unmatched_share, 0.01)
    assert.equal(coverage.passed, true)
  })

  test('the two counters of the same calls are not added up', () => {
    const coverage = standInCoverage(stats(0), stats(100, { abc: 2 }, 2))
    assert.equal(coverage.unmatched_calls, 2)
    assert.equal(coverage.unmatched_share, 0.02)
    assert.equal(coverage.passed, true)
  })

  test('either counter alone is enough to see the calls', () => {
    assert.equal(standInCoverage(stats(0), stats(100, { abc: 9 }, 0)).passed, false)
    assert.equal(standInCoverage(stats(0), stats(100, {}, 9)).passed, false)
  })

  test('a run the stand-in never saw is not covered', () => {
    const coverage = standInCoverage(stats(700), stats(700))
    assert.equal(coverage.total_calls, 0)
    assert.equal(coverage.unmatched_share, NOT_MEASURED)
    assert.equal(coverage.passed, false)
    assert.equal(standInCoverage(null, stats(1)).passed, false)
    assert.equal(standInCoverage(stats(1), null).passed, false)
  })

  test('a stand-in that restarted measured nothing, however good the numbers look', () => {
    const coverage = standInCoverage(
      { total_calls: 1_000, uptime_seconds: 500, unmatched_instructions: { a: 50 } },
      { total_calls: 1_500, uptime_seconds: 20, unmatched_instructions: { a: 2 } },
    )
    assert.equal(coverage.passed, false)
    assert.equal(coverage.reason, 'stand_in_restarted')
  })

  test('a reply built for a known operation counts as no recording', () => {
    const coverage = standInCoverage(
      { total_calls: 0 },
      { synthesised_replies: { 'op.a': 3, unmatched: 2 }, total_calls: 100, unmatched_instructions: { x: 2 } },
    )
    assert.equal(coverage.unmatched_calls, 5)
    assert.equal(coverage.passed, false)
  })

  test('counts the documents the application refused', () => {
    const coverage = standInCoverage(
      { evidence_errors: 1, total_calls: 0 },
      { evidence_errors: 4, total_calls: 10 },
    )
    assert.equal(coverage.evidence_errors, 3)
  })

  test('names what was built on the spot and what was dropped', () => {
    const coverage = standInCoverage(
      stats(10, {}, 0, { synthesised_replies: { 'op.a': 2 }, tool_calls_dropped: { 'op.b': 1 } }),
      stats(110, {}, 0, { synthesised_replies: { 'op.a': 5, 'op.c': 1 }, tool_calls_dropped: { 'op.b': 3 } }),
    )
    assert.deepEqual(coverage.synthesised_replies, { 'op.a': 3, 'op.c': 1 })
    assert.equal(coverage.synthesised_replies_total, 4)
    assert.deepEqual(coverage.tool_calls_dropped, { 'op.b': 2 })
    assert.equal(coverage.tool_calls_dropped_total, 2)
  })
})

describe('the comparison with a base report', () => {
  const values = (overrides = {}) => ({
    connections_per_container_max: { api: 20, worker: 10 },
    loop_lag_p99_ms: 10,
    setup: { config_digest: 'abc', latency_scale: 1 },
    sse_lag_p50_ms: 50,
    sse_lag_p95_ms: 100,
    stage: 'l1',
    submit_p95_ms: 200,
    tx_per_provider_call_net: 100,
    ...overrides,
  })
  const rule = (judgement, id) => judgement.rules.find((row) => row.id === id)

  test('the same values hold', () => {
    const judgement = judgeBaseline(values(), [values()])
    assert.equal(judgement.status, 'passed')
    assert.ok(judgement.rules.every((row) => row.status === 'passed'))
  })

  test('threads: up to ten percent more than the base holds, beyond it is red', () => {
    const base = values({ threads_per_container_max: { worker: 150 } })
    const at = (value) => judgeBaseline(values({ threads_per_container_max: { worker: value } }), [base])
    assert.equal(rule(at(165), 'threads_per_container:worker').status, 'passed')
    assert.equal(rule(at(165.1), 'threads_per_container:worker').status, 'failed')
  })

  test('threads: a base from before the measure says nothing', () => {
    const judgement = judgeBaseline(values({ threads_per_container_max: { worker: 900 } }), [values()])
    assert.equal(rule(judgement, 'threads_per_container:worker'), undefined)
  })

  test('threads: what the base has and this run did not measure is red', () => {
    const base = values({ threads_per_container_max: { worker: 150 } })
    const judgement = judgeBaseline(values(), [base])
    assert.equal(rule(judgement, 'threads_per_container:worker').status, 'failed')
  })

  test('threads: only the processes this code sizes decide', () => {
    const base = values({ threads_per_container_max: { collaboration: 13, worker: 150 } })
    const judgement = judgeBaseline(
      values({ threads_per_container_max: { collaboration: 20, worker: 150 } }),
      [base],
    )
    assert.equal(rule(judgement, 'threads_per_container:collaboration'), undefined)
    assert.equal(rule(judgement, 'threads_per_container:worker').status, 'passed')
  })

  test('transactions: up to ten percent more per provider call holds, beyond it is red', () => {
    const near = judgeBaseline(values({ tx_per_provider_call_net: 110 }), [values()])
    assert.equal(rule(near, 'transactions_per_provider_call').status, 'passed')
    assert.equal(rule(near, 'transactions_per_provider_call').limit, 110)
    assert.equal(near.status, 'passed')
    const above = judgeBaseline(values({ tx_per_provider_call_net: 110.1 }), [values()])
    assert.equal(rule(above, 'transactions_per_provider_call').status, 'failed')
    assert.equal(above.status, 'failed')
    assert.equal(judgeBaseline(values({ tx_per_provider_call_net: 50 }), [values()]).status, 'passed')
  })

  test('transactions: a negative net count is no measurement and no improvement', () => {
    const judgement = judgeBaseline(values({ tx_per_provider_call_net: -254.29 }), [values()])
    assert.equal(rule(judgement, 'transactions_per_provider_call').status, 'failed')
    assert.equal(rule(judgement, 'transactions_per_provider_call').reason, NOT_MEASURED)
    assert.equal(judgement.status, 'failed')
  })

  test('transactions per run scatter too much to decide and are only reported', () => {
    const judgement = judgeBaseline(values({ tx_per_run_net: 9_000 }), [values({ tx_per_run_net: 100 })])
    assert.equal(judgement.status, 'passed')
  })

  test('connections: two bases grant the spread between them, identical bases none', () => {
    const bases = [
      values({ connections_per_container_max: { api: 38, worker: 19 } }),
      values({ connections_per_container_max: { api: 36, worker: 19 } }),
    ]
    const inside = judgeBaseline(values({ connections_per_container_max: { api: 40, worker: 19 } }), bases)
    assert.equal(rule(inside, 'connections_per_container:api').limit, 40)
    assert.equal(rule(inside, 'connections_per_container:api').status, 'passed')
    const outside = judgeBaseline(values({ connections_per_container_max: { api: 41, worker: 19 } }), bases)
    assert.equal(rule(outside, 'connections_per_container:api').status, 'failed')
    const exact = judgeBaseline(values({ connections_per_container_max: { api: 38, worker: 20 } }), bases)
    assert.equal(rule(exact, 'connections_per_container:worker').limit, 19)
    assert.equal(rule(exact, 'connections_per_container:worker').status, 'failed')
  })

  test('the setup is the same whatever order its keys arrived in', () => {
    const one = values({ setup: { containers: { api: 2, worker: 2 }, latency_scale: 1 } })
    const other = values({ setup: { latency_scale: 1, containers: { worker: 2, api: 2 } } })
    assert.equal(rule(judgeBaseline(one, [other]), 'same_setup').status, 'passed')
    const different = values({ setup: { latency_scale: 1, containers: { worker: 4, api: 2 } } })
    assert.equal(rule(judgeBaseline(one, [different]), 'same_setup').status, 'failed')
  })

  test('connections: one more in a container is red, fewer holds', () => {
    const more = judgeBaseline(
      values({ connections_per_container_max: { api: 21, worker: 10 } }),
      [values()],
    )
    assert.equal(rule(more, 'connections_per_container:api').status, 'failed')
    assert.equal(rule(more, 'connections_per_container:worker').status, 'passed')
    assert.equal(more.status, 'failed')
    const fewer = judgeBaseline(
      values({ connections_per_container_max: { api: 19, worker: 10 } }),
      [values()],
    )
    assert.equal(fewer.status, 'passed')
  })

  test('connections: a container the base never had counts as more', () => {
    const judgement = judgeBaseline(
      values({ connections_per_container_max: { api: 20, worker: 10, broker: 1 } }),
      [values()],
    )
    assert.equal(rule(judgement, 'connections_per_container:broker').status, 'failed')
  })

  test('connections: a pseudo service is no comparison, a missing reading is no zero', () => {
    const local = judgeBaseline(
      values({ connections_per_container_max: { api: 20, local: 9, unknown: 3, worker: 10 } }),
      [values()],
    )
    assert.equal(rule(local, 'connections_per_container:local'), undefined)
    assert.equal(rule(local, 'connections_per_container:unknown'), undefined)
    assert.equal(local.status, 'passed')
    const missing = judgeBaseline(
      values({ connections_per_container_max: { api: 20 } }),
      [values()],
    )
    assert.equal(rule(missing, 'connections_per_container:worker').status, 'failed')
    assert.equal(rule(missing, 'connections_per_container:worker').reason, NOT_MEASURED)
  })

  test('delivery delay: a median within two histogram buckets of a single base is a warning', () => {
    const near = judgeBaseline(values({ sse_lag_p50_ms: 53 }), [values()])
    assert.equal(rule(near, 'sse_lag_p50').status, 'warning')
    assert.equal(rule(near, 'sse_lag_p50').tolerance, 4)
    assert.equal(rule(near, 'sse_lag_p50').limit, 54)
    assert.equal(near.status, 'passed')
    assert.deepEqual(near.warnings, ['sse_lag_p50'])
    assert.equal(judgeBaseline(values({ sse_lag_p50_ms: 54.1 }), [values()]).status, 'failed')
    assert.deepEqual(judgeBaseline(values({ sse_lag_p50_ms: 50 }), [values()]).warnings, [])
  })

  test('delivery delay: the tail warns against the base and only the model limit makes it red', () => {
    assert.equal(SSE_LAG_P95_LIMIT_MS, 1000)
    const worse = judgeBaseline(values({ sse_lag_p95_ms: 900 }), [values()])
    assert.equal(rule(worse, 'sse_lag_p95').status, 'warning')
    assert.equal(rule(worse, 'sse_lag_p95').limit, 1000)
    assert.equal(worse.status, 'passed')
    assert.deepEqual(worse.warnings, ['sse_lag_p95'])
    assert.equal(rule(judgeBaseline(values({ sse_lag_p95_ms: 1000 }), [values()]), 'sse_lag_p95').status, 'warning')
    const above = judgeBaseline(values({ sse_lag_p95_ms: 1000.1 }), [values({ sse_lag_p95_ms: 5000 })])
    assert.equal(rule(above, 'sse_lag_p95').status, 'failed')
    assert.equal(above.status, 'failed')
    assert.deepEqual(judgeBaseline(values(), [values()]).warnings, [])
  })

  test('the other times warn and never redden the gate', () => {
    const judgement = judgeBaseline(values({ submit_p95_ms: 900 }), [values()])
    assert.equal(rule(judgement, 'submit_p95').status, 'warning')
    assert.equal(judgement.status, 'passed')
    assert.deepEqual(judgement.warnings, ['submit_p95'])
    assert.equal(rule(judgeBaseline(values({ submit_p95_ms: 205 }), [values()]), 'submit_p95').status, 'passed')
  })

  test('another setup is no base', () => {
    const judgement = judgeBaseline(
      values({ setup: { config_digest: 'abc', latency_scale: 0.1 } }),
      [values()],
    )
    assert.equal(rule(judgement, 'same_setup').status, 'failed')
    assert.equal(judgement.status, 'failed')
    assert.equal(rule(judgeBaseline(values({ setup: null }), [values({ setup: null })]), 'same_setup').status, 'failed')
  })

  test('delivery delay: two bases grant the spread of the median between them', () => {
    const bases = [values({ sse_lag_p50_ms: 100 }), values({ sse_lag_p50_ms: 130 })]
    const inside = judgeBaseline(values({ sse_lag_p50_ms: 160 }), bases)
    assert.equal(rule(inside, 'sse_lag_p50').tolerance, 30)
    assert.equal(rule(inside, 'sse_lag_p50').limit, 160)
    assert.equal(rule(inside, 'sse_lag_p50').status, 'warning')
    assert.equal(rule(judgeBaseline(values({ sse_lag_p50_ms: 130 }), bases), 'sse_lag_p50').status, 'passed')
    const outside = judgeBaseline(values({ sse_lag_p50_ms: 160.5 }), bases)
    assert.equal(rule(outside, 'sse_lag_p50').status, 'failed')
    const swapped = judgeBaseline(values({ sse_lag_p50_ms: 160 }), [...bases].reverse())
    assert.equal(rule(swapped, 'sse_lag_p50').limit, 160)
  })

  test('loop lag: above fifty milliseconds is red whatever the base did', () => {
    assert.equal(LOOP_LAG_P99_LIMIT_MS, 50)
    const above = judgeBaseline(values({ loop_lag_p99_ms: 50.1 }), [values({ loop_lag_p99_ms: 500 })])
    assert.equal(rule(above, 'loop_lag_p99').status, 'failed')
    assert.equal(above.status, 'failed')
    const at = judgeBaseline(values({ loop_lag_p99_ms: 50 }), [values()])
    assert.equal(rule(at, 'loop_lag_p99').status, 'passed')
  })

  test('what was not measured cannot show that a rule holds', () => {
    for (const [overrides, id] of [
      [{ tx_per_provider_call_net: null }, 'transactions_per_provider_call'],
      [{ sse_lag_p50_ms: null }, 'sse_lag_p50'],
      [{ sse_lag_p95_ms: null }, 'sse_lag_p95'],
      [{ loop_lag_p99_ms: null }, 'loop_lag_p99'],
    ]) {
      const judgement = judgeBaseline(values(overrides), [values()])
      assert.equal(rule(judgement, id).status, 'failed', id)
      assert.equal(rule(judgement, id).reason, NOT_MEASURED, id)
      assert.equal(judgement.status, 'failed', id)
    }
    const empty = judgeBaseline(
      values({ connections_per_container_max: {} }),
      [values({ connections_per_container_max: {} })],
    )
    assert.equal(rule(empty, 'connections_per_container').status, 'failed')
  })

  test('another stage is no base', () => {
    const judgement = judgeBaseline(values(), [values({ stage: 'smoke' })])
    assert.equal(rule(judgement, 'same_stage').status, 'failed')
    assert.equal(judgement.status, 'failed')
  })

  test('a report stopped early or with a red scenario is no base', () => {
    const comparison = values()
    assert.throws(() => normalizeBaseline({ comparison, stop_reason: 'interrupted' }), /stopped early/)
    assert.throws(
      () => normalizeBaseline({ comparison, scenarios: [{ id: 'x', status: 'failed' }] }),
      /red scenario/,
    )
    assert.deepEqual(normalizeBaseline({ comparison, scenarios: [{ id: 'x', status: 'passed' }] }), [comparison])
  })

  test('the starting limits are shown next to the value, also when unmeasured', () => {
    assert.deepEqual(
      compareWithLimits({ submit_p95_ms: 900, sse_lag_p95_ms: null }, { source: 'text', sse_lag_p95_ms: 1000, submit_p95_ms: 300 }),
      [
        { exceeded: NOT_MEASURED, id: 'sse_lag_p95_ms', limit: 1000, value: NOT_MEASURED },
        { exceeded: true, id: 'submit_p95_ms', limit: 300, value: 900 },
      ],
    )
  })

  test('a base file holds one report or several', () => {
    const report = { comparison: values() }
    assert.deepEqual(normalizeBaseline(report), [values()])
    assert.deepEqual(normalizeBaseline([report, report]), [values(), values()])
    assert.deepEqual(normalizeBaseline({ reports: [report, report, report] }), [values(), values(), values()])
    assert.throws(() => normalizeBaseline([]), /at least one report/)
    assert.throws(() => normalizeBaseline({ stage: 'l1' }), /no comparison values/)
  })

  test('a report red only against an older base is still a base, red elsewhere is not', () => {
    const comparison = values()
    const againstOlder = { id: 'run-load.baseline-holds', status: 'failed' }
    assert.deepEqual(normalizeBaseline({ comparison, scenarios: [againstOlder] }), [comparison])
    assert.throws(
      () => normalizeBaseline({ comparison, scenarios: [againstOlder, { id: 'run-load.no-residue', status: 'failed' }] }),
      /red scenario/,
    )
  })

  test('three bases grant the whole range between them, whatever their order', () => {
    const lag = (...values_) => values_.map((value) => values({ sse_lag_p50_ms: value }))
    for (const bases of [lag(100, 130, 110), lag(130, 110, 100), lag(110, 100, 130)]) {
      const inside = judgeBaseline(values({ sse_lag_p50_ms: 160 }), bases)
      assert.equal(rule(inside, 'sse_lag_p50').tolerance, 30)
      assert.equal(rule(inside, 'sse_lag_p50').limit, 160)
      assert.equal(rule(inside, 'sse_lag_p50').status, 'warning')
      assert.equal(rule(judgeBaseline(values({ sse_lag_p50_ms: 160.5 }), bases), 'sse_lag_p50').status, 'failed')
    }
    const peaks = [38, 36, 40].map((api) => values({ connections_per_container_max: { api, worker: 10 } }))
    const atLimit = judgeBaseline(values({ connections_per_container_max: { api: 44, worker: 10 } }), peaks)
    assert.equal(rule(atLimit, 'connections_per_container:api').limit, 44)
    assert.equal(rule(atLimit, 'connections_per_container:api').status, 'passed')
    const beyond = judgeBaseline(values({ connections_per_container_max: { api: 45, worker: 10 } }), peaks)
    assert.equal(rule(beyond, 'connections_per_container:api').status, 'failed')
  })
})

describe('residue', () => {
  test('nothing left and nothing failed holds', () => {
    const residue = judgeResidue({ accounts: 0, tables: { runs: 0, users: 0 } })
    assert.equal(residue.total, 0)
    assert.equal(residue.passed, true)
  })

  test('a remaining account is red', () => {
    const residue = judgeResidue({ accounts: 1, tables: { runs: 0 } })
    assert.equal(residue.total, 1)
    assert.equal(residue.passed, false)
  })

  test('a remaining row is red', () => {
    const residue = judgeResidue({ accounts: 0, tables: { runs: 3, users: 0 } })
    assert.equal(residue.total, 3)
    assert.equal(residue.passed, false)
  })

  test('an account count nobody got is no zero', () => {
    const residue = judgeResidue({ accounts: null, tables: { runs: 0 } })
    assert.equal(residue.total, NOT_MEASURED)
    assert.equal(residue.accounts, NOT_MEASURED)
    assert.equal(residue.passed, false)
  })

  test('a table that could not be read may hold anything, so it is red', () => {
    const residue = judgeResidue({ accounts: 0, tables: { runs: null, users: 0 } })
    assert.equal(residue.tables.runs, NOT_MEASURED)
    assert.equal(residue.total, NOT_MEASURED)
    assert.equal(residue.passed, false)
  })

  test('a failed cleanup step is red even when nothing was found afterwards', () => {
    const residue = judgeResidue({ accounts: 0, cleanupFailures: 1, tables: {} })
    assert.equal(residue.total, 0)
    assert.equal(residue.passed, false)
  })

  test('residue reddens its scenario', () => {
    const verdict = (residue) => judgeScenarios({
      coverage: { passed: true },
      provisioned: true,
      residue,
      successRatioValue: 1,
      workloadRan: true,
    }).find(({ id }) => id === SCENARIO.residue).status
    assert.equal(verdict(judgeResidue({ accounts: 0 })), 'passed')
    assert.equal(verdict(judgeResidue({ accounts: 2 })), 'failed')
    assert.equal(verdict(judgeResidue({ accounts: null })), 'failed')
  })
})

describe('a process that restarted inside a window', () => {
  test('leaves the loop lag of that window instead of garbling it', () => {
    const limits_ms = [1, 10, 100]
    const reading = (counts) => ({ counts, limits_ms })
    const lag = loopLagBetween(
      { kept: reading([1, 1, 0]), restarted: reading([10, 0, 0]) },
      { kept: reading([1, 3, 0]), restarted: reading([1, 30, 0]) },
    )
    assert.deepEqual(Object.keys(lag), ['kept'])
  })

  test('has no rate for a counter that fell', () => {
    assert.equal(ratePerSecond(900, 5, 10), null)
    assert.equal(ratePerSecond(5, 15, 10), 1)
  })
})

describe('one stopped worker', () => {
  const stop = (overrides = {}) => judgeWorkerStop({
    activeRootsAtStop: 25,
    failedAfterStop: 0,
    restarted: true,
    stopped: true,
    takeovers: 3,
    unfinished: 0,
    ...overrides,
  })

  test('passes when its runs were handed over and none was lost', () => {
    assert.equal(stop().passed, true)
  })

  for (const [name, overrides, check] of [
    ['a run lost', { unfinished: 1 }, 'no_run_lost'],
    ['a run failed after the stop', { failedAfterStop: 1 }, 'no_run_failed_after_the_stop'],
    ['takeovers it could not read', { takeovers: 'not_measured' }, 'runs_handed_over'],
    ['no run handed over', { takeovers: 0 }, 'runs_handed_over'],
    ['nothing in flight at the stop', { activeRootsAtStop: 0 }, 'runs_in_flight_at_the_stop'],
    ['a worker that never stopped', { stopped: false }, 'worker_stopped'],
    ['a worker that never came back', { restarted: false }, 'worker_restarted'],
  ]) {
    test(`fails on ${name}`, () => {
      const verdict = stop(overrides)
      assert.equal(verdict.passed, false)
      assert.deepEqual(verdict.checks.filter((row) => !row.passed).map((row) => row.id), [check])
    })
  }

  test('its scenario exists only where a worker was stopped', () => {
    const verdicts = (fault) => judgeScenarios({
      coverage: { passed: true },
      fault,
      provisioned: true,
      successRatioValue: 1,
      workloadRan: true,
    }).filter(({ id }) => id === SCENARIO.workerStop).map(({ status }) => status)
    assert.deepEqual(verdicts(undefined), [])
    assert.deepEqual(verdicts(null), [])
    assert.deepEqual(verdicts(stop()), ['passed'])
    assert.deepEqual(verdicts(stop({ unfinished: 2 })), ['failed'])
  })
})

describe('scenario verdicts', () => {
  const judged = (overrides = {}) => Object.fromEntries(
    judgeScenarios({
      coverage: { passed: true },
      events: { duplicated: 0, live: 40, lost: 0, runs_checked: 3 },
      loadApplied: { passed: true },
      provisioned: true,
      residue: judgeResidue({ accounts: 0 }),
      successRatioValue: 0.95,
      workloadRan: true,
      ...overrides,
    }).map(({ id, status }) => [id, status]),
  )

  test('a clean run passes everything it reached', () => {
    assert.deepEqual(judged(), {
      [SCENARIO.events]: 'passed',
      [SCENARIO.loadApplied]: 'passed',
      [SCENARIO.provisioned]: 'passed',
      [SCENARIO.residue]: 'passed',
      [SCENARIO.standIn]: 'passed',
      [SCENARIO.workload]: 'passed',
    })
  })

  test('the workload holds at nine in ten and fails below', () => {
    assert.equal(judged({ successRatioValue: 0.9 })[SCENARIO.workload], 'passed')
    assert.equal(judged({ successRatioValue: 0.899 })[SCENARIO.workload], 'failed')
    assert.equal(judged({ successRatioValue: NOT_MEASURED })[SCENARIO.workload], 'failed')
  })

  test('a scenario the run never reached stays open instead of red', () => {
    const verdicts = judged({ provisioned: false, residue: null, workloadRan: false })
    assert.deepEqual(verdicts, { [SCENARIO.provisioned]: 'failed' })
  })

  test('the base scenario exists only when a base was given', () => {
    assert.equal(SCENARIO.baseline in judged(), false)
    assert.equal(judged({ baseline: { status: 'passed' } })[SCENARIO.baseline], 'passed')
    assert.equal(judged({ baseline: { status: 'failed' } })[SCENARIO.baseline], 'failed')
  })

  test('a lost or repeated event is red, and so is a run without a single account', () => {
    const verdict = (events) => judged({ events })[SCENARIO.events]
    assert.equal(verdict({ duplicated: 0, live: 40, lost: 1, runs_checked: 3 }), 'failed')
    assert.equal(verdict({ duplicated: 1, live: 40, lost: 0, runs_checked: 3 }), 'failed')
    assert.equal(verdict({ duplicated: 0, live: 40, lost: 0, runs_checked: 0 }), 'failed')
  })

  test('events that only ever arrived by polling are no working stream', () => {
    assert.equal(judged({ events: { duplicated: 0, live: 0, lost: 0, runs_checked: 3 } })[SCENARIO.events], 'failed')
  })

  test('a stage that did not reach the stack is red', () => {
    assert.equal(judged({ loadApplied: { passed: false } })[SCENARIO.loadApplied], 'failed')
    assert.equal(judged({ loadApplied: null })[SCENARIO.loadApplied], 'failed')
  })

  test('coverage reaches its scenario', () => {
    assert.equal(judged({ coverage: { passed: false } })[SCENARIO.standIn], 'failed')
    assert.equal(judged({ coverage: null })[SCENARIO.standIn], 'failed')
  })
})

describe('the cancel probe', () => {
  const at = (seconds, inFlight) => ({ in_flight: inFlight, seconds })

  test('reads how soon the calls of the cancel are gone', () => {
    assert.deepEqual(
      cancelSpread([at(1, 40), at(2, 30), at(3, 20), at(4, 2), at(5, 1), at(6, 0)], 40),
      { seconds_to_half: 3, seconds_to_p95: 4, seconds_to_quiet: 6, status: 'measured' },
    )
  })

  test('a call that started after the cancel counts as held', () => {
    assert.deepEqual(
      cancelSpread([at(1, 2), at(2, 0), at(3, 4), at(4, 0)], 4),
      { seconds_to_half: 4, seconds_to_p95: 4, seconds_to_quiet: 4, status: 'measured' },
    )
  })

  test('calls that outlive the wait claim nothing they did not show', () => {
    assert.deepEqual(
      cancelSpread([at(1, 40), at(2, 19), at(3, 19)], 40),
      { seconds_to_half: 2, seconds_to_p95: NOT_MEASURED, seconds_to_quiet: NOT_MEASURED, status: NOT_MEASURED },
    )
    assert.equal(cancelSpread([], 4).status, NOT_MEASURED)
  })
})

describe('whether the load reached the stack', () => {
  const facts = (overrides = {}) => ({
    activeRootsMedian: 48,
    directFailures: 0,
    directStarted: 100,
    httpFailures: 0,
    phaseOverruns: [],
    rejected: 0,
    requests: 10_000,
    resultsUnreadable: 0,
    runStreamDrops: 0,
    runStreams: 400,
    started: 300,
    stopReason: null,
    strayFailures: 0,
    tabsOpen: 300,
    tabsSignedOut: 0,
    tabsWithStream: 300,
    targetRoots: 50,
    userStreamDrops: 0,
    ...overrides,
  })
  const failing = (overrides, minimum) => judgeLoadApplied(facts(overrides), CONFIG.load_applied, minimum)
    .checks.filter((check) => !check.passed).map((check) => check.id)

  test('a stage that arrived in full passes', () => {
    assert.equal(judgeLoadApplied(facts(), CONFIG.load_applied).passed, true)
  })

  test('a machine that stood still during a phase measured nothing, whatever else held', () => {
    assert.deepEqual(failing({ phaseOverruns: ['plateau', 'decay'] }), ['phases_as_planned'])
  })

  test('a phase the comparison reads counts as overrun only well beyond what its timers scatter', () => {
    const stage = { rest_seconds: 60, idle_seconds: 120, plateau_seconds: 900 }
    const report = (overrides = {}) => ({
      idle: { seconds: 120.3 },
      plateau: { seconds: 900.1 },
      rest: { seconds: 60.3 },
      ...overrides,
    })
    assert.deepEqual(phaseOverruns(report(), stage), [])
    assert.deepEqual(phaseOverruns(report({ plateau: { seconds: 950 } }), stage), [])
    assert.deepEqual(phaseOverruns(report({ plateau: { seconds: 950.1 } }), stage), ['plateau'])
    assert.deepEqual(phaseOverruns(report({ idle: { seconds: 131.1 } }), stage), ['idle'])
    assert.deepEqual(phaseOverruns(report({ rest: { seconds: 68.1 } }), stage), ['rest'])
    assert.deepEqual(phaseOverruns(report({ ramp: { seconds: 427 } }), stage), [])
    assert.deepEqual(phaseOverruns({ rest: { seconds: 60 } }, stage), [])
  })

  test('each way of losing the load has its own check', () => {
    for (const [overrides, id] of [
      [{ activeRootsMedian: 44 }, 'active_roots'],
      [{ activeRootsMedian: undefined }, 'active_roots'],
      [{ tabsWithStream: 299 }, 'every_tab_has_its_stream'],
      [{ tabsOpen: 0, tabsWithStream: 0 }, 'every_tab_has_its_stream'],
      [{ tabsSignedOut: 1 }, 'no_tab_signed_out'],
      [{ rejected: 7 }, 'rejected_share'],
      [{ httpFailures: 11 }, 'http_failure_share'],
      [{ directFailures: 6 }, 'direct_failure_share'],
      [{ runStreamDrops: 21 }, 'run_stream_drop_share'],
      [{ userStreamDrops: 16 }, 'user_stream_drop_share'],
      [{ resultsUnreadable: 1 }, 'results_readable'],
      [{ stopReason: 'interrupted' }, 'no_stop_reason'],
      [{ strayFailures: 1 }, 'no_stray_failure'],
    ]) {
      assert.deepEqual(failing(overrides), [id], JSON.stringify(overrides))
    }
  })

  test('300 rejected of 400 submits is red although most of the rest finished', () => {
    assert.deepEqual(failing({ rejected: 300, started: 100 }), ['rejected_share'])
  })

  test('a stage may set its own share of active roots', () => {
    assert.deepEqual(failing({ activeRootsMedian: 2, targetRoots: 3 }, 0.5), [])
    assert.deepEqual(failing({ activeRootsMedian: 1, targetRoots: 3 }, 0.5), ['active_roots'])
  })

  test('counts requests and failures from the route table, errors included', () => {
    assert.deepEqual(requestTotals({
      'GET /a': { errors: { ECONNRESET: 2 }, n: 10, status_5xx: { 500: 1 } },
      'GET /b': { errors: {}, n: 5, status_5xx: {} },
    }), { failures: 3, requests: 17 })
    assert.deepEqual(requestTotals(undefined), { failures: 0, requests: 0 })
  })
})

describe('the report', () => {
  const snapshot = (at, commits, sessions, events, valkey, calls) => ({
    at,
    pg: { commits, sessions, wal: commits * 10 },
    run_events: events,
    stand_in: { total_calls: calls },
    valkey,
  })

  const metricsWith = () => {
    const metrics = createPhasedMetrics(['provision', 'rest', 'idle', 'ramp', 'plateau', 'decay', 'cancel_probe', 'cleanup'])
    metrics.setPhase('plateau')
    metrics.count('runs_started', 10)
    metrics.count('runs_completed', 9)
    metrics.count('runs_failed', 1)
    metrics.count('events_live', 500)
    metrics.count('events_replayed', 100)
    metrics.recordLag(40)
    return metrics
  }

  const input = (overrides = {}) => ({
    coverage: standInCoverage(
      { total_calls: 0 },
      {
        calls_per_operation: { unmatched: 1 },
        synthesised_replies: { 'op.a': 2 },
        tool_calls_dropped: { 'op.b': 3 },
        total_calls: 100,
        unmatched_instructions: { abc: 1 },
      },
    ),
    finishedAt: 'end',
    metrics: metricsWith(),
    mix: { planned: {}, started: {} },
    phases: {
      decay: { end: snapshot(1_300, 3_500, 58, 1_400, 9_000, 100), gauges: createPhaseGauges().summary(), start: snapshot(1_200, 3_000, 55, 1_300, 8_000, 90) },
      idle: { end: snapshot(1_000, 200, 12, 0, 500, 0), gauges: createPhaseGauges().summary(), start: snapshot(900, 100, 10, 0, 400, 0) },
      plateau: { end: snapshot(1_200, 3_000, 55, 1_300, 8_000, 90), gauges: createPhaseGauges().summary(), start: snapshot(1_100, 1_000, 25, 300, 3_000, 40) },
      ramp: { end: snapshot(1_100, 1_000, 25, 300, 3_000, 40), gauges: createPhaseGauges().summary(), start: snapshot(1_000, 200, 12, 0, 500, 0) },
      rest: { end: snapshot(800, 50, 8, 0, 300, 0), gauges: createPhaseGauges().summary(), start: snapshot(700, 0, 6, 0, 200, 0) },
    },
    probes: {
      final: {
        api: { service: 'api', values: { loop_lag: { n: 9, p99_ms: 12 } } },
        worker: { service: 'worker', values: null },
      },
    },
    provisioning: { complete: true },
    residue: judgeResidue({ accounts: 0 }),
    runData: {
      queued_by_lane: { 'research/standard': { p50: 0.5, p95: 7.25, runs: 12 } },
      queued_p50: 0.5,
      queued_p95: 7.25,
      runs: 12,
    },
    runId: 'inqv-report-test-1',
    stack: { project: 'p' },
    stage: 'smoke',
    startedAt: 'start',
    ...overrides,
  })

  test('holds the idle, ramp and plateau values apart', () => {
    const report = buildReport(input())
    assert.equal(report.idle.transactions_per_second, 1)
    assert.equal(report.ramp.transactions_per_second, 8)
    assert.equal(report.plateau.transactions_per_second, 20)
    assert.equal(report.plateau.events_written_per_second, 10)
    assert.equal(report.plateau.events_delivered_per_second, 6)
    assert.equal(report.plateau.valkey_commands_per_second, 50)
    assert.equal(report.plateau.connections_new_per_minute, 18)
    assert.deepEqual(report.events_per_second.written, { idle: 0, plateau: 10, ramp: 3 })
    assert.deepEqual(report.valkey_commands_per_second, { idle: 1, plateau: 50, ramp: 25 })
  })

  test('nets the plateau against the idle rate per finished run', () => {
    const report = buildReport(input())
    // From the ramp's start to the decay's end: 3,300 commits in 300 s, 1 per second idle.
    assert.equal(report.transactions_per_run_net.work_delta, 3_300)
    assert.equal(report.transactions_per_run_net.work_seconds, 300)
    assert.equal(report.transactions_per_run_net.idle_rate_per_second, 1)
    assert.equal(report.transactions_per_run_net.runs_finished, 10)
    assert.equal(report.transactions_per_run_net.value, 300)
    assert.equal(report.comparison.tx_per_run_net, 300)
  })

  test('counts commits and rollbacks per provider call, net of the idle rate', () => {
    const withRollbacks = (phase, first, last) => ({
      ...phase,
      end: { ...phase.end, pg: { ...phase.end.pg, rollbacks: last } },
      start: { ...phase.start, pg: { ...phase.start.pg, rollbacks: first } },
    })
    const phases = input().phases
    phases.idle = withRollbacks(phases.idle, 0, 100)
    phases.plateau = withRollbacks(phases.plateau, 0, 1_000)
    const report = buildReport(input({ phases }))
    // Plateau 2,000 commits and 1,000 rollbacks in 100 s; idle 2 per second; 50 calls.
    assert.equal(report.transactions_per_provider_call_net.transactions, 3_000)
    assert.equal(report.transactions_per_provider_call_net.provider_calls, 50)
    assert.equal(report.transactions_per_provider_call_net.idle_rate_per_second, 2)
    assert.equal(report.transactions_per_provider_call_net.value, 56)
    assert.equal(report.comparison.tx_per_provider_call_net, 56)
  })

  test('without rollbacks or a provider call that value is not claimed', () => {
    assert.equal(buildReport(input()).comparison.tx_per_provider_call_net, null)
    assert.equal(buildReport(input()).transactions_per_provider_call_net.value, NOT_MEASURED)
  })

  test('every run that finished from the first to the last counts as a run', () => {
    const metrics = createPhasedMetrics(['provision', 'rest', 'idle', 'ramp', 'plateau', 'decay', 'cancel_probe', 'cleanup'])
    for (const [phase, count] of [['ramp', 2], ['plateau', 5], ['decay', 3]]) {
      metrics.setPhase(phase)
      metrics.count('runs_completed', count)
    }
    assert.equal(buildReport(input({ metrics })).transactions_per_run_net.runs_finished, 10)
  })

  test('a counter nobody read stays not measured', () => {
    const phases = input().phases
    phases.plateau = { gauges: createPhaseGauges().summary(), start: phases.plateau.start, end: { at: 1_200, pg: null, run_events: null, stand_in: null, valkey: null } }
    phases.decay = { gauges: createPhaseGauges().summary(), start: phases.decay.start, end: { at: 1_300, pg: null, run_events: null, stand_in: null, valkey: null } }
    const report = buildReport(input({ phases }))
    assert.equal(report.plateau.transactions_per_second, NOT_MEASURED)
    assert.equal(report.plateau.valkey_commands_per_second, NOT_MEASURED)
    assert.equal(report.plateau.events_written_per_second, NOT_MEASURED)
    assert.equal(report.transactions_per_run_net.value, NOT_MEASURED)
    assert.equal(report.comparison.tx_per_run_net, null)
  })

  test('a probe that did not answer stays not measured', () => {
    const report = buildReport(input())
    assert.deepEqual(report.probes.final.worker, { service: 'worker', status: NOT_MEASURED })
    assert.equal(report.probes.final.api.loop_lag.p99_ms, 12)
    assert.equal(report.comparison.loop_lag_p99_ms, null)
  })

  test('the comparison takes the loop lag of the plateau alone', () => {
    const phases = input().phases
    const limits_ms = [10, 50, 100]
    phases.plateau = {
      ...phases.plateau,
      end: { ...phases.plateau.end, loop: { api: { counts: [1_000, 0, 50, 0], limits_ms } } },
      start: { ...phases.plateau.start, loop: { api: { counts: [1_000, 0, 0, 0], limits_ms } } },
    }
    const report = buildReport(input({ phases }))
    assert.equal(report.comparison.loop_lag_p99_ms, 100)
    assert.equal(report.plateau.loop_lag.api.share_over_limit, 1)
  })

  test('the comparison holds what makes two runs comparable and their times', () => {
    const metrics = metricsWith()
    metrics.recordGroup('submit', 120)
    const setup = { config_digest: 'abc', latency_scale: 1 }
    const report = buildReport(input({ metrics, setup }))
    assert.equal(report.comparison.setup, setup)
    assert.ok(Math.abs(report.comparison.submit_p95_ms - 120) < 5)
    assert.equal(report.comparison.result_p95_ms, null)
  })

  test('the comparison holds the median of the delivery delay apart from its tail', () => {
    const metrics = metricsWith()
    for (let index = 0; index < 90; index += 1) metrics.recordLag(100)
    for (let index = 0; index < 10; index += 1) metrics.recordLag(2_000)
    const { comparison } = buildReport(input({ metrics }))
    assert.ok(Math.abs(comparison.sse_lag_p50_ms - 100) < 5, String(comparison.sse_lag_p50_ms))
    assert.ok(Math.abs(comparison.sse_lag_p95_ms - 2_000) < 100, String(comparison.sse_lag_p95_ms))
  })

  test('the starting limits and the failed requests are in the report', () => {
    const report = buildReport(input({ limits: { source: 'text', tx_per_run_net: 1 } }))
    assert.deepEqual(report.limits, [{ exceeded: true, id: 'tx_per_run_net', limit: 1, value: 300 }])
    assert.deepEqual(report.http, { failures: 0, requests: 0 })
  })

  test('rollbacks are read like the commits', () => {
    const phases = input().phases
    phases.plateau = {
      ...phases.plateau,
      end: { ...phases.plateau.end, pg: { ...phases.plateau.end.pg, rollbacks: 50 } },
      start: { ...phases.plateau.start, pg: { ...phases.plateau.start.pg, rollbacks: 30 } },
    }
    assert.equal(buildReport(input({ phases })).plateau.rollbacks_per_second, 0.2)
  })

  test('takes the slot wait from the database and marks its absence', () => {
    assert.deepEqual(buildReport(input()).slot_wait_seconds, {
      by_lane: { 'research/standard': { p50: 0.5, p95: 7.25, runs: 12 } },
      p50: 0.5,
      p95: 7.25,
      runs: 12,
    })
    assert.deepEqual(buildReport(input({ runData: null })).slot_wait_seconds, {
      by_lane: NOT_MEASURED,
      p50: NOT_MEASURED,
      p95: NOT_MEASURED,
      runs: NOT_MEASURED,
    })
  })

  test('the counted line carries its fields in a fixed order', () => {
    const line = formatSummaryLine(buildReport(input()))
    assert.equal(
      line,
      'RUN-LOAD SUMMARY stage=smoke roots_started=10 completed=9 failed=1 success_ratio=0.9 '
      + 'tx_per_run_net=300 tx_per_call_net=not_measured conn_new_per_min=18 sse_lag_p50_ms=40 sse_lag_p95_ms=40 residue=0 unmatched_share=0.03 '
      + 'synthesised_replies=2 tool_calls_dropped=3 cancelled=0 unfinished=0 '
      + 'events_lost=0 events_duplicated=0 rejected=0 http_failures=0 '
      + 'active_roots_median=not_measured loop_lag_p99_ms=not_measured latency_scale=not_measured',
    )
  })

  test('the counted line says not measured instead of a zero', () => {
    const line = formatSummaryLine(buildReport(input({
      coverage: null,
      metrics: createPhasedMetrics(['provision', 'rest', 'idle', 'ramp', 'plateau', 'decay', 'cancel_probe', 'cleanup']),
      residue: null,
    })))
    assert.match(line, /success_ratio=not_measured/)
    assert.match(line, /residue=not_measured/)
    assert.match(line, /sse_lag_p50_ms=not_measured/)
    assert.match(line, /sse_lag_p95_ms=not_measured/)
    assert.match(line, /unmatched_share=not_measured/)
    assert.match(line, /synthesised_replies=not_measured/)
  })

  test('counts what the streams lost and repeated', () => {
    const metrics = metricsWith()
    metrics.count('events_lost', 2)
    metrics.count('events_duplicated', 1)
    metrics.count('runs_with_full_account', 9)
    assert.deepEqual(buildReport(input({ metrics })).events_delivery, {
      duplicated: 1,
      live: 500,
      lost: 2,
      runs_checked: 9,
    })
  })

  test('a page costs what the idle phase holds above the rest phase, per tab', () => {
    const gauges = (threads, anon) => {
      const phase = createPhaseGauges()
      phase.recordContainer({ name: 'stack-api-1', service: 'api' }, { anon_bytes: anon, cpu_usec: 0, threads }, 1)
      phase.recordContainer({ name: 'stack-api-2', service: 'api' }, { anon_bytes: anon, cpu_usec: 0, threads }, 1)
      return phase.summary()
    }
    const phases = input().phases
    phases.rest = { ...phases.rest, gauges: gauges(10, 1_000) }
    phases.idle = { ...phases.idle, gauges: gauges(20, 1_500) }
    const report = buildReport(input({ phases, provisioning: { complete: true, tabs_open: 10 } }))
    assert.deepEqual(report.unit_costs.per_tab.threads, { api: 2 })
    assert.deepEqual(report.unit_costs.per_tab.anon_bytes, { api: 100 })
    // Idle 1 transaction per second, rest 0.5, over ten tabs.
    assert.equal(report.unit_costs.per_tab.transactions_per_second, 0.05)
  })

  test('a slot and an active root cost what the plateau holds above the idle phase', () => {
    const gauges = (threads, anon, running) => {
      const phase = createPhaseGauges()
      phase.recordContainer({ name: 'stack-worker-1', service: 'worker' }, { anon_bytes: anon, cpu_usec: 0, threads }, 1)
      phase.recordPostgres({ active: 0, by_client: {}, idle_in_tx: 0, lock_waits: [], locks_waiting: 0, runs: { running }, total: 0 }, () => ({ name: 'x', service: 'x' }))
      return phase.summary()
    }
    const phases = input().phases
    phases.idle = { ...phases.idle, gauges: gauges(100, 2_000, 0) }
    phases.plateau = { ...phases.plateau, gauges: gauges(200, 7_000, 50) }
    const costs = buildReport(input({ activeRoots: { max: 30, median: 25, target: 30 }, phases })).unit_costs
    assert.equal(costs.per_running_slot.count, 50)
    assert.deepEqual(costs.per_running_slot.threads, { worker: 2 })
    assert.deepEqual(costs.per_running_slot.anon_bytes, { worker: 100 })
    assert.equal(costs.per_running_slot.transactions_per_second, 0.38)
    // The same increase over the roots, which a parked root or a child does not change.
    assert.equal(costs.per_active_root.count, 25)
    assert.deepEqual(costs.per_active_root.threads, { worker: 4 })
    assert.equal(buildReport(input({ phases })).unit_costs.per_active_root.count, NOT_MEASURED)
  })

  test('statements are read per service over its replicas, per phase and per unit', () => {
    assert.deepEqual(statementsPerService({
      'stack-api-1': { service: 'api', values: { statements: 10 } },
      'stack-api-2': { service: 'api', values: { statements: 5 } },
      'stack-worker-1': { service: 'worker', values: null },
    }), { api: 15 })
    const withStatements = (phase, first, last) => ({
      ...phase,
      end: { ...phase.end, statements: { api: last } },
      start: { ...phase.start, statements: { api: first } },
    })
    const phases = input().phases
    phases.rest = withStatements(phases.rest, 0, 100)
    phases.idle = withStatements(phases.idle, 100, 300)
    const report = buildReport(input({ phases, provisioning: { tabs_open: 10 } }))
    assert.deepEqual(report.rest.statements_per_second, { api: 1 })
    assert.deepEqual(report.idle.statements_per_second, { api: 2 })
    assert.deepEqual(report.unit_costs.per_tab.statements_per_second, { api: 0.1 })
  })

  test('a phase nobody could read leaves the unit costs unmeasured', () => {
    const phases = input().phases
    phases.rest = { gauges: createPhaseGauges().summary() }
    const report = buildReport(input({ phases, provisioning: { tabs_open: 10 } }))
    assert.equal(report.unit_costs.per_tab.transactions_per_second, NOT_MEASURED)
    assert.equal(buildReport(input({ provisioning: { tabs_open: 0 } })).unit_costs.per_tab.transactions_per_second, NOT_MEASURED)
  })

  test('the report carries the axes of the load next to the values', () => {
    assert.deepEqual(buildReport(input({ loadMatrix: { axis: 1 } })).load_matrix, { axis: 1 })
    assert.equal(buildReport(input()).load_matrix, NOT_MEASURED)
  })

  test('a question, an address or a password in a report is found', () => {
    const needles = { password: 'geheim-123456', 'question plain': 'Erkläre verständlich' }
    assert.deepEqual(findForbiddenContent('{"a":1}', needles), [])
    assert.deepEqual(findForbiddenContent('{"a":"user@example.invalid"}', needles), ['email address'])
    assert.deepEqual(findForbiddenContent('{"a":"geheim-123456"}', needles), ['password'])
    assert.deepEqual(findForbiddenContent('Erkläre verständlich, wie', needles), ['question plain'])
  })

  test('a built report holds none of it', () => {
    const text = JSON.stringify(buildReport(input()))
    const needles = Object.fromEntries(
      Object.entries(CONFIG.questions).map(([key, question]) => [key, question]),
    )
    assert.deepEqual(findForbiddenContent(text, needles), [])
  })
})
