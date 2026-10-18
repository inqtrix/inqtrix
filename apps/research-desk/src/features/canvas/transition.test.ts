import { describe, expect, it } from 'vitest'

import { canvasTransitionKey } from './transition'
import type { CanvasViewDescriptor } from './types'

describe('canvasTransitionKey', () => {
  it('shares one key across run-internal drill-in (taskId ignored)', () => {
    // Overview <-> task detail is the page PUSH inside the run view; a
    // differing host key would unmount the pane and kill the mounted
    // list layer (scroll + focus restore).
    const overview = { runId: 'r1', view: 'run' } as CanvasViewDescriptor
    const detail = {
      runId: 'r1',
      taskId: 't3',
      view: 'run',
    } as CanvasViewDescriptor
    expect(canvasTransitionKey(overview)).toBe(canvasTransitionKey(detail))
  })

  it('separates different runs and non-run views', () => {
    const runA = { runId: 'r1', view: 'run' } as CanvasViewDescriptor
    const runB = { runId: 'r2', view: 'run' } as CanvasViewDescriptor
    expect(canvasTransitionKey(runA)).not.toBe(canvasTransitionKey(runB))
    expect(canvasTransitionKey(null)).toBe('empty')
  })
})
