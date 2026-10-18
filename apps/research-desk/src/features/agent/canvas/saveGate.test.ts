import { readFileSync } from 'node:fs'
import path from 'node:path'
import { describe, expect, it } from 'vitest'

import { createCanvasSaveGate } from './saveGate'

describe('canvas save gate', () => {
  it('holds the loaded body, so an edit reverted before its save is no change', () => {
    const gate = createCanvasSaveGate()
    gate.observeServerBody('loaded', false)

    expect(gate.holds('loaded')).toBe(true)
    expect(gate.holds('edited')).toBe(false)
  })

  it('holds the submitted body while its save is in flight, not the stale server body', () => {
    const gate = createCanvasSaveGate()
    gate.observeServerBody('loaded', false)
    gate.submit('first edit')
    // The reload of the old body arrives before the save's own result.
    gate.observeServerBody('loaded', false)

    expect(gate.holds('first edit')).toBe(true)
    expect(gate.holds('loaded')).toBe(false)

    gate.settle('first edit', true)
    expect(gate.holds('first edit')).toBe(true)
  })

  it('ignores a server body that arrives while an edit waits unsent', () => {
    const gate = createCanvasSaveGate()
    gate.observeServerBody('loaded', false)
    gate.observeServerBody('agent rewrite', true)

    expect(gate.holds('loaded')).toBe(true)
    expect(gate.holds('agent rewrite')).toBe(false)
  })

  it('knows nothing after a save that did not land, until the server speaks again', () => {
    const gate = createCanvasSaveGate()
    gate.observeServerBody('loaded', false)
    gate.submit('rejected edit')
    gate.settle('rejected edit', false)

    expect(gate.holds('rejected edit')).toBe(false)
    expect(gate.holds('loaded')).toBe(false)

    gate.observeServerBody('current', false)
    expect(gate.holds('current')).toBe(true)
  })

  it('holds nothing before any body is known', () => {
    expect(createCanvasSaveGate().holds('')).toBe(false)
  })
})

// Source pin (DOM-less lane): the document view consults the gate before it
// queues a save; the browser lane drives the mounted canvas.
describe('DocumentCanvasView save wiring', () => {
  const source = readFileSync(path.resolve(__dirname, './views.tsx'), 'utf-8')

  it('drops a change back to the held body and its queued save', () => {
    const start = source.indexOf('const handleChange = useCallback(')
    const handler = source.slice(start, source.indexOf('CANVAS_SAVE_DEBOUNCE_MS)', start))
    const guard = handler.slice(handler.indexOf('if (saveGate.holds(markdown)) {'))
    expect(guard).toContain('pendingRef.current = null')
    expect(handler.indexOf('if (saveGate.holds(markdown)) {'))
      .toBeLessThan(handler.indexOf('pendingRef.current = markdown'))
  })
})
