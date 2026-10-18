import { beforeEach, describe, expect, it } from 'vitest'
import {
  DEMO_SHARED_RUN_ID,
  listDemoShares,
  resetDemoShares,
  updateDemoShare,
} from './demoShares'

describe('demo direct shares', () => {
  beforeEach(resetDemoShares)

  it('keeps pending consent distinct and revisions permission changes', () => {
    const records = listDemoShares('run', DEMO_SHARED_RUN_ID)
    const pending = records.find((record) => record.accepted_at === null)
    expect(pending).toBeDefined()

    const updated = updateDemoShare(
      pending!.id,
      pending!.permission === 'view' ? 'edit' : 'view',
      pending!.revision,
    )

    expect(updated.accepted_at).toBeNull()
    expect(updated.revision).toBe(pending!.revision + 1)
    expect(() => updateDemoShare(
      updated.id,
      'view',
      pending!.revision,
    )).toThrow('zwischenzeitlich')
  })
})
