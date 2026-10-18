import { describe, expect, it } from 'vitest'

import { reportStructuralIdentity } from './ReportPanel'

describe('report structural identity', () => {
  it('rearms the surface when one run changes from live status to a report', () => {
    expect(reportStructuralIdentity('run-a', 'running')).not.toBe(
      reportStructuralIdentity('run-a', 'completed-with-report'),
    )
  })

  it('keeps updates within one live mode on the same identity', () => {
    expect(reportStructuralIdentity('run-a', 'running')).toBe(
      reportStructuralIdentity('run-a', 'running'),
    )
  })

  it('uses a stable immediate identity for the empty state', () => {
    expect(reportStructuralIdentity(null, 'empty')).toBe('report:empty')
  })
})
