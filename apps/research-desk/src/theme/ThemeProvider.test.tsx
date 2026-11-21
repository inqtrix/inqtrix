import { readFileSync } from 'node:fs'
import { runInNewContext } from 'node:vm'
import { renderToStaticMarkup } from 'react-dom/server'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { preferenceChangeFromStorage, ThemeProvider, useTheme } from './ThemeProvider'

const bootstrap = readFileSync(new URL('../../index.html', import.meta.url), 'utf8')
  .match(/<script>([\s\S]*?)<\/script>/)?.[1]

function CurrentPreset() {
  return <span>{useTheme().preset}</span>
}

function CurrentLastChoices() {
  const { lastAgentTier, lastResearchProfile, preset } = useTheme()
  return <span>{`${lastResearchProfile}|${lastAgentTier}|${preset}`}</span>
}

type ThemeValue = ReturnType<typeof useTheme>

/** Hands the context value out of a static render, so a test can call its setters. */
function Capture({ into }: { into: { value: ThemeValue | null } }) {
  into.value = useTheme()
  return null
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

describe('the last picks on this device', () => {
  function stored(values: Record<string, string>) {
    vi.stubGlobal('localStorage', {
      getItem: (key: string) => values[key] ?? null,
      setItem: vi.fn(),
    })
    return renderToStaticMarkup(<ThemeProvider><CurrentLastChoices /></ThemeProvider>)
  }

  it('starts from the picks the device saved', () => {
    expect(stored({
      'inqtrix.researchDesk.lastAgentTier': 'tief',
      'inqtrix.researchDesk.lastResearchProfile': 'compact',
    })).toBe('<span>compact|tief|petrol</span>')
  })

  it.each([
    ['nothing saved', {}],
    ['values from another vocabulary', {
      'inqtrix.researchDesk.lastAgentTier': 'compact',
      'inqtrix.researchDesk.lastResearchProfile': 'gruendlich',
    }],
    ['unknown values', {
      'inqtrix.researchDesk.lastAgentTier': 'standard',
      'inqtrix.researchDesk.lastResearchProfile': 'turbo',
    }],
  ])('reads %s as no pick', (_label, values) => {
    expect(stored(values)).toBe('<span>||petrol</span>')
  })

  it('reads a blocked device storage as no pick and the defaults', () => {
    // Private mode and blocked site data throw on every access; the desk
    // then starts with its defaults instead of failing to render.
    vi.stubGlobal('localStorage', {
      getItem() { throw new Error('Storage blocked') },
      setItem() { throw new Error('Storage blocked') },
    })
    expect(renderToStaticMarkup(<ThemeProvider><CurrentLastChoices /></ThemeProvider>))
      .toBe('<span>||petrol</span>')
  })

  it('a refused save never undoes a pick', () => {
    // The pick takes effect first; a storage that refuses to keep it (full,
    // blocked) costs only the memory on this device.
    vi.stubGlobal('localStorage', {
      getItem: () => null,
      setItem() { throw new Error('QuotaExceededError') },
    })
    const captured: { value: ThemeValue | null } = { value: null }
    renderToStaticMarkup(<ThemeProvider><Capture into={captured} /></ThemeProvider>)
    const theme = captured.value
    if (!theme) throw new Error('No theme context captured')
    expect(() => {
      theme.setLastAgentTier('schnell')
      theme.setLastResearchProfile('compact')
      theme.setAgentMemoryEnabled(true)
      theme.setAgentModelTier('fast')
      theme.setChatModelTier('mid')
      theme.setContrastMode('high')
      theme.setPreset('sage')
      theme.setTheme('dark')
      theme.setUserBubbleTone('mint')
    }).not.toThrow()
  })
})

describe('a preference another tab saved', () => {
  it.each([
    ['inqtrix.researchDesk.theme', 'dark', { field: 'theme', value: 'dark' }],
    ['inqtrix.researchDesk.themePreset', 'sage', { field: 'preset', value: 'sage' }],
    ['inqtrix.researchDesk.contrastMode', 'high', { field: 'contrastMode', value: 'high' }],
    ['inqtrix.researchDesk.userBubbleTone', 'mint', { field: 'userBubbleTone', value: 'mint' }],
    ['inqtrix.researchDesk.agentMemoryEnabled', 'true', { field: 'agentMemoryEnabled', value: true }],
    ['inqtrix.researchDesk.agentMemoryEnabled', 'false', { field: 'agentMemoryEnabled', value: false }],
    ['inqtrix.researchDesk.chatModelTier', 'mid', { field: 'chatModelTier', value: 'mid' }],
    // No model preference is a choice of its own, and it is taken over too.
    ['inqtrix.researchDesk.chatModelTier', '', { field: 'chatModelTier', value: '' }],
    ['inqtrix.researchDesk.agentModelTier', 'fast', { field: 'agentModelTier', value: 'fast' }],
    ['inqtrix.researchDesk.lastResearchProfile', 'compact', { field: 'lastResearchProfile', value: 'compact' }],
    ['inqtrix.researchDesk.lastAgentTier', 'tief', { field: 'lastAgentTier', value: 'tief' }],
  ] as const)('takes over %s = %s', (key, value, change) => {
    expect(preferenceChangeFromStorage(key, value)).toEqual(change)
  })

  it.each([
    ['inqtrix.researchDesk.theme', 'neon'],
    ['inqtrix.researchDesk.themePreset', 'gold'],
    ['inqtrix.researchDesk.agentMemoryEnabled', 'yes'],
    ['inqtrix.researchDesk.agentModelTier', 'turbo'],
    // A last pick is never taken back to no pick by another tab.
    ['inqtrix.researchDesk.lastResearchProfile', ''],
    ['inqtrix.researchDesk.lastResearchProfile', 'tief'],
    ['inqtrix.researchDesk.lastAgentTier', 'deep'],
    ['inqtrix.researchDesk.lastAgentTier', null],
    ['inqtrix.researchDesk.locale', 'de'],
    ['inqtrix.research.auth-recovery.v1', '{}'],
    [null, null],
  ] as const)('ignores %s = %s', (key, value) => {
    expect(preferenceChangeFromStorage(key, value)).toBeNull()
  })
})
