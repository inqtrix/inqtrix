import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import {
  clearAllPlanDrafts,
  clearPlanDraftsForRun,
  identityChanged,
  planDraftStorageKey,
  readPlanDraft,
  writePlanDraft,
} from './planDraftStorage'
import type { AgentPlanDraft } from './usePlanApproval'

function draft(overrides: Partial<AgentPlanDraft> = {}): AgentPlanDraft {
  return {
    assumptions: [],
    rejectNote: 'Bitte ohne Websuche.',
    rejectPending: false,
    reportRequirementTouched: false,
    reportGuidance: 'Als Tabelle, fuer den Vorstand.',
    reportRuleIds: ['rule-1'],
    successCriteria: [],
    summaryMarkdown: '',
    tasks: [],
    version: 1,
    ...overrides,
  }
}

/** The node-only suite has no browser storage — a Map is a faithful
 * stand-in for the three methods this module uses. */
function fakeStorage(): Storage {
  const values = new Map<string, string>()
  return {
    get length() {
      return values.size
    },
    key: (index: number) => [...values.keys()][index] ?? null,
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => {
      values.set(key, value)
    },
    removeItem: (key: string) => {
      values.delete(key)
    },
  } as unknown as Storage
}

const original = Object.getOwnPropertyDescriptor(globalThis, 'localStorage')

function installStorage(storage: Storage | undefined): void {
  Object.defineProperty(globalThis, 'localStorage', {
    configurable: true,
    value: storage,
    writable: true,
  })
}

beforeEach(() => {
  installStorage(fakeStorage())
})

afterEach(() => {
  if (original) Object.defineProperty(globalThis, 'localStorage', original)
  else delete (globalThis as { localStorage?: Storage }).localStorage
})

describe('plan draft storage', () => {
  it('gives a reload back the unsent decision', () => {
    // Everything the user put into a pending gate survives a reload:
    // edited questions, the requirement, attached rules, the note.
    writePlanDraft('run-1', draft())
    const restored = readPlanDraft('run-1', 1)
    expect(restored?.reportGuidance).toBe('Als Tabelle, fuer den Vorstand.')
    expect(restored?.reportRuleIds).toEqual(['rule-1'])
    expect(restored?.rejectNote).toBe('Bitte ohne Websuche.')
  })

  it('never applies a draft to a different plan version', () => {
    // A replan proposes different tasks; the old draft's edits would
    // land on tasks that no longer exist.
    writePlanDraft('run-1', draft())
    expect(readPlanDraft('run-1', 2)).toBeNull()
  })

  it('keeps runs apart', () => {
    writePlanDraft('run-1', draft())
    expect(readPlanDraft('run-2', 1)).toBeNull()
  })

  it('forgets the draft once the gate is decided', () => {
    writePlanDraft('run-1', draft())
    clearPlanDraftsForRun('run-1')
    expect(readPlanDraft('run-1', 1)).toBeNull()
  })

  it('sweeps the versions a reject-and-replan left behind', () => {
    // Reject -> replan -> reject again: each round writes its own key.
    // Only clearing the decided version would leave the earlier ones in
    // the browser forever.
    writePlanDraft('run-1', draft({ version: 1 }))
    writePlanDraft('run-1', draft({ version: 2 }))
    writePlanDraft('run-2', draft({ version: 1 }))
    clearPlanDraftsForRun('run-1')
    expect(readPlanDraft('run-1', 1)).toBeNull()
    expect(readPlanDraft('run-1', 2)).toBeNull()
    // Another run's open gate is none of this decision's business.
    expect(readPlanDraft('run-2', 1)).not.toBeNull()
  })

  it('ignores a stored value that no longer has the draft shape', () => {
    // Half a draft would render a broken gate. Absent is the safe read:
    // the gate falls back to the agent's plan.
    globalThis.localStorage.setItem(
      planDraftStorageKey('run-1', 1),
      JSON.stringify({ version: 1, reportGuidance: 'x' }),
    )
    expect(readPlanDraft('run-1', 1)).toBeNull()
  })

  it('survives an unparseable entry', () => {
    globalThis.localStorage.setItem(planDraftStorageKey('run-1', 1), '{oops')
    expect(readPlanDraft('run-1', 1)).toBeNull()
  })

  it('survives a store that refuses to work', () => {
    // Private mode and blocked site data throw on access. A gate that
    // cannot remember must still be a working gate.
    installStorage({
      getItem: () => {
        throw new Error('blocked')
      },
      setItem: () => {
        throw new Error('blocked')
      },
      removeItem: () => {
        throw new Error('blocked')
      },
    } as unknown as Storage)
    expect(() => writePlanDraft('run-1', draft())).not.toThrow()
    expect(readPlanDraft('run-1', 1)).toBeNull()
    expect(() => clearPlanDraftsForRun('run-1')).not.toThrow()
  })

  it('survives an environment without storage at all', () => {
    installStorage(undefined)
    expect(() => writePlanDraft('run-1', draft())).not.toThrow()
    expect(readPlanDraft('run-1', 1)).toBeNull()
  })

  it('keys by run and version', () => {
    expect(planDraftStorageKey('run-1', 3)).toBe(
      'inqtrix.agent.plan-draft:run-1:v3',
    )
  })
})

describe('drafts do not cross an identity boundary', () => {
  it('keeps what the SAME person typed across a reload', () => {
    // The whole point of the draft: a first observation is not a change.
    expect(identityChanged(undefined, 'user-a')).toBe(false)
  })

  it('drops them when another account takes over the profile', () => {
    // The auth layer reloads the document on an identity transition, but
    // localStorage survives that reload.
    expect(identityChanged('user-a', 'user-b')).toBe(true)
  })

  it('drops them on logout', () => {
    expect(identityChanged('user-a', null)).toBe(true)
  })

  it('keeps them while the same person stays signed in', () => {
    expect(identityChanged('user-a', 'user-a')).toBe(false)
  })

  it('sweeps every run and version at once', () => {
    writePlanDraft('run-1', { ...draft(), version: 1 })
    writePlanDraft('run-1', { ...draft(), version: 2 })
    writePlanDraft('run-2', { ...draft(), version: 1 })
    clearAllPlanDrafts()
    expect(readPlanDraft('run-1', 1)).toBeNull()
    expect(readPlanDraft('run-1', 2)).toBeNull()
    expect(readPlanDraft('run-2', 1)).toBeNull()
  })
})
