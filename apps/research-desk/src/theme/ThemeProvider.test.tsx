import { readFileSync } from 'node:fs'
import { runInNewContext } from 'node:vm'
import { renderToStaticMarkup } from 'react-dom/server'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { ThemeProvider, useTheme } from './ThemeProvider'

const bootstrap = readFileSync(new URL('../../index.html', import.meta.url), 'utf8')
  .match(/<script>([\s\S]*?)<\/script>/)?.[1]

function CurrentPreset() {
  return <span>{useTheme().preset}</span>
}

afterEach(() => vi.unstubAllGlobals())

describe('theme startup', () => {
  it.each([
    [null, 'petrol'],
    ['unknown', 'petrol'],
    ['petrol', 'petrol'],
    ['standard', 'standard'],
    ['slate', 'slate'],
    ['graphite', 'graphite'],
    ['sage', 'sage'],
  ])('keeps bootstrap and provider in agreement for stored preset %s', (stored, expected) => {
    const getItem = (key: string) => key.endsWith('.themePreset') ? stored : null
    vi.stubGlobal('localStorage', { getItem })
    const root = { classList: { toggle: vi.fn() }, dataset: {} as Record<string, string>, style: {} }
    if (!bootstrap) throw new Error('Missing prepaint theme bootstrap')
    runInNewContext(bootstrap, {
      document: { documentElement: root }, localStorage: { getItem }, window: {},
    })
    expect(root.dataset.themePreset).toBe(expected)
    expect(renderToStaticMarkup(<ThemeProvider><CurrentPreset /></ThemeProvider>))
      .toBe(`<span>${expected}</span>`)
  })

  it('uses Petrol before paint when storage access is unavailable', () => {
    const root = { classList: { toggle: vi.fn() }, dataset: {} as Record<string, string>, style: {} }
    if (!bootstrap) throw new Error('Missing prepaint theme bootstrap')
    runInNewContext(bootstrap, {
      document: { documentElement: root },
      localStorage: { getItem() { throw new Error('Storage blocked') } }, window: {},
    })
    expect(root.dataset.themePreset).toBe('petrol')
  })
})
