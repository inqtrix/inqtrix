import { describe, expect, it } from 'vitest'
import {
  clearRunning,
  clearRuns,
  markError,
  markErrors,
  markManyRunning,
  markRunning,
  runErrors,
  runningIds,
  type EditorRunStateMap,
} from './editorRunState'

describe('editorRunState', () => {
  it('marks an id running and lists it', () => {
    const map = markRunning({}, 'a')
    expect(runningIds(map)).toEqual(['a'])
    expect(runErrors(map)).toEqual({})
  })

  it('clears a prior error when the same id starts running again', () => {
    const errored = markError({}, 'a', 'boom')
    const rerun = markRunning(errored, 'a')
    expect(runErrors(rerun)).toEqual({})
    expect(runningIds(rerun)).toEqual(['a'])
  })

  it('replaces a running id with an error', () => {
    const map = markError(markRunning({}, 'a'), 'a', 'failed')
    expect(runningIds(map)).toEqual([])
    expect(runErrors(map)).toEqual({ a: 'failed' })
  })

  it('marks and clears many ids', () => {
    const running = markManyRunning({}, ['a', 'b', 'c'])
    expect(runningIds(running).sort()).toEqual(['a', 'b', 'c'])
    const cleared = clearRuns(running, ['a', 'c'])
    expect(runningIds(cleared)).toEqual(['b'])
  })

  it('merges a batch of errors', () => {
    const map = markErrors(markManyRunning({}, ['a', 'b']), { a: 'x', b: 'y' })
    expect(runErrors(map)).toEqual({ a: 'x', b: 'y' })
    expect(runningIds(map)).toEqual([])
  })

  it('returns the same reference when clearing absent ids', () => {
    const map: EditorRunStateMap = markRunning({}, 'a')
    expect(clearRuns(map, ['z'])).toBe(map)
  })

  it('clearRunning removes running ids but keeps errored ids', () => {
    const map = markError(markRunning({}, 'a'), 'b', 'failed')
    const next = clearRunning(map, ['a', 'b'])
    expect(runningIds(next)).toEqual([])
    expect(runErrors(next)).toEqual({ b: 'failed' })
  })
})
