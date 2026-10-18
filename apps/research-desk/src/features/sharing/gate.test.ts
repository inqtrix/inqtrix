import { describe, expect, it } from 'vitest'
import type { InqtrixCapabilities } from '@/features/researchRuns/types'
import { isSharingEnabled } from './gate'

const caps = (sharing: boolean) =>
  ({ features: { sharing } }) as unknown as InqtrixCapabilities

describe('isSharingEnabled', () => {
  it('is true in demo regardless of capability or session', () => {
    expect(
      isSharingEnabled({
        authMode: 'none',
        capabilities: null,
        isDemo: true,
        sessionStatus: 'anonymous',
      }),
    ).toBe(true)
  })

  it('requires the capability, a cookie-session mode, and authentication', () => {
    expect(
      isSharingEnabled({
        authMode: 'oidc',
        capabilities: caps(true),
        isDemo: false,
        sessionStatus: 'authenticated',
      }),
    ).toBe(true)
  })

  it('is false when the backend does not advertise sharing', () => {
    expect(
      isSharingEnabled({
        authMode: 'oidc',
        capabilities: caps(false),
        isDemo: false,
        sessionStatus: 'authenticated',
      }),
    ).toBe(false)
  })

  it('is false for single-operator modes even when authenticated', () => {
    for (const authMode of ['none', 'apikey'] as const) {
      expect(
        isSharingEnabled({
          authMode,
          capabilities: caps(true),
          isDemo: false,
          sessionStatus: 'authenticated',
        }),
      ).toBe(false)
    }
  })

  it('is false until the cookie session is authenticated', () => {
    expect(
      isSharingEnabled({
        authMode: 'local',
        capabilities: caps(true),
        isDemo: false,
        sessionStatus: 'anonymous',
      }),
    ).toBe(false)
  })
})
