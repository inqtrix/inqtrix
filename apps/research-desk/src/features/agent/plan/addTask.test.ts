import { describe, expect, it } from 'vitest'

import {
  buildUserPlanTask,
  newTaskParams,
  webProfileOptionsForTier,
  withUserPlanTask,
} from './addTask'
import type { AgentPlanDraft } from './usePlanApproval'

describe('webProfileOptionsForTier', () => {
  it('offers only what the tier ceiling admits', () => {
    expect(webProfileOptionsForTier('gruendlich')).toEqual([
      'schnell',
      'compact',
    ])
    expect(webProfileOptionsForTier('tief')).toEqual([
      'schnell',
      'compact',
      'deep',
    ])
    expect(webProfileOptionsForTier('schnell')).toEqual([])
  })

  it('mirrors the legacy EXACT pin without a tier', () => {
    // Legacy runs pin the child profile server-side (depth-derived);
    // offering more would let the validator reject the edit.
    expect(webProfileOptionsForTier(undefined)).toEqual(['compact'])
    expect(webProfileOptionsForTier(undefined, 'deep')).toEqual(['deep'])
  })
})

describe('newTaskParams', () => {
  it('derives tier-policy defaults', () => {
    expect(newTaskParams('web_research', { tier: 'gruendlich' })).toEqual({
      profile: 'schnell',
    })
    expect(newTaskParams('web_research', { tier: 'tief' })).toEqual({
      profile: 'compact',
    })
    expect(newTaskParams('rag_query', { tier: 'tief' })).toEqual({
      profile: 'gruendlich',
    })
  })

  it('falls back to legacy depth semantics without a tier', () => {
    expect(newTaskParams('web_research', { depth: 'deep' })).toEqual({
      profile: 'deep',
    })
    expect(newTaskParams('web_research', {})).toEqual({ profile: 'compact' })
    expect(newTaskParams('rag_query', {})).toEqual({ profile: 'standard' })
    expect(newTaskParams('web_instant', {})).toEqual({})
  })
})

describe('buildUserPlanTask', () => {
  it('attaches picked collections to a knowledge task', () => {
    const task = buildUserPlanTask({
      collectionIds: ['kc_eu'],
      kind: 'rag_query',
      taskId: 't_user_1',
      text: 'Compliance-Anforderungen konsolidieren',
    })
    expect(task.params).toEqual({
      collection_ids: ['kc_eu'],
      profile: 'standard',
    })
    expect(task.queries).toEqual(['Compliance-Anforderungen konsolidieren'])
  })

  it('never leaks collections onto web tasks', () => {
    const task = buildUserPlanTask({
      collectionIds: ['kc_eu'],
      kind: 'web_instant',
      taskId: 't_user_2',
      text: 'Aktuelle Marktlage',
    })
    expect(task.params).toEqual({})
  })
})

describe('withUserPlanTask', () => {
  const draft = (): AgentPlanDraft => ({
    assumptions: [],
    reportGuidance: '',
    reportRuleIds: [],
    rejectPending: false,
    reportRequirementTouched: false,
    rejectNote: '',
    successCriteria: [],
    summaryMarkdown: '',
    tasks: [
      {
        budget: {},
        dependsOn: [],
        expectedOutput: '',
        gapIds: [],
        objective: '',
        params: {},
        queries: [],
        taskId: 's',
        title: 'Synthese',
        toolKind: 'synthesis',
      },
    ],
    version: 1,
  })

  it('inserts before synthesis and extends its dependencies', () => {
    const task = buildUserPlanTask({
      collectionIds: [],
      kind: 'web_instant',
      taskId: 't_user_3',
      text: 'Frage',
    })
    const next = withUserPlanTask(draft(), task)
    expect(next.tasks.map((item) => item.taskId)).toEqual(['t_user_3', 's'])
    expect(next.tasks[1].dependsOn).toEqual(['t_user_3'])
  })
})
