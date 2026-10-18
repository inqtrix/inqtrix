import { renderToString } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'

import {
  createStructuralRenderRegistry,
  StructuralLoadBoundary,
} from './StructuralLoadBoundary'

describe('StructuralLoadBoundary first commit', () => {
  it('starts a cold pending region quietly before expensive target work mounts', () => {
    const html = renderToString(
      <StructuralLoadBoundary
        fallback={<div data-testid="fallback">Laden</div>}
        identity="thread-a"
        phase="pending"
      >
        <div data-testid="target">Nachrichten</div>
      </StructuralLoadBoundary>,
    )

    expect(html).toContain('data-structural-region=""')
    expect(html).toContain('data-structural-state="pending"')
    expect(html).not.toContain('data-testid="target"')
    expect(html).not.toContain('data-structural-layer="staged"')
    expect(html).not.toContain('data-testid="fallback"')
    expect(html).not.toContain('data-structural-fallback=""')
  })

  it('does not render a fallback for a warm ready target', () => {
    const html = renderToString(
      <StructuralLoadBoundary
        fallback={<div data-testid="fallback">Laden</div>}
        identity="report-cached"
        phase="ready"
      >
        <div data-testid="target">Fertiger Bericht</div>
      </StructuralLoadBoundary>,
    )

    expect(html).toContain('data-structural-state="staging"')
    expect(html).toContain('data-testid="target"')
    expect(html).not.toContain('data-testid="fallback"')
  })

  it.each(['refreshing', 'empty', 'error'] as const)(
    'renders %s immediately without fallback or staging',
    (phase) => {
      const html = renderToString(
        <StructuralLoadBoundary
          fallback={<div data-testid="fallback">Laden</div>}
          identity={`surface-${phase}`}
          phase={phase}
        >
          <div data-testid="target">Sichtbare Wahrheit</div>
        </StructuralLoadBoundary>,
      )

      expect(html).toContain(`data-structural-state="${phase}"`)
      expect(html).toContain('data-structural-layer="visible"')
      expect(html).toContain('data-testid="target"')
      expect(html).not.toContain('data-testid="fallback"')
      expect(html).not.toContain('data-structural-layer="staged"')
    },
  )
})

describe('structural render blocker registry', () => {
  it('holds until every distinct blocker reaches a terminal commit', () => {
    const registry = createStructuralRenderRegistry()
    const first = registry.add(Symbol('mermaid'))
    const second = registry.add(Symbol('image'))

    expect(registry.getSnapshot()).toBe(2)
    first()
    expect(registry.getSnapshot()).toBe(1)
    second()
    expect(registry.getSnapshot()).toBe(0)
  })

  it('reference-counts one token and makes releases idempotent', () => {
    const registry = createStructuralRenderRegistry()
    const token = Symbol('strict-mode-consumer')
    const first = registry.add(token)
    const second = registry.add(token)

    expect(registry.getSnapshot()).toBe(1)
    first()
    first()
    expect(registry.getSnapshot()).toBe(1)
    second()
    expect(registry.getSnapshot()).toBe(0)
  })

  it('clears aborted work and notifies subscribers exactly once', () => {
    const registry = createStructuralRenderRegistry()
    const listener = vi.fn()
    registry.subscribe(listener)
    registry.add(Symbol('aborted-mermaid'))
    registry.add(Symbol('aborted-image'))
    listener.mockClear()

    registry.clear()
    registry.clear()

    expect(registry.getSnapshot()).toBe(0)
    expect(listener).toHaveBeenCalledOnce()
  })
})
