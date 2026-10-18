import { describe, expect, it } from 'vitest'
import {
  CHAT_STEP_FALLBACK_TIMEOUT_MS,
  CLIENT_WAIT_MARGIN_MS,
  EDITOR_FALLBACK_TIMEOUT_MS,
  deriveChatStepTimeoutMs,
  deriveEditorAbortMs,
  isMissingServerTimeouts,
} from './clientTimeouts'
import type { InqtrixCapabilities } from './types'

function caps(timeouts?: InqtrixCapabilities['timeouts']): InqtrixCapabilities {
  return {
    algorithms: [],
    features: { embedding_provider: false, knowledge: false,
    multi_stack: false, openapi: false },
    ...(timeouts ? { timeouts } : {}),
  }
}

describe('deriveEditorAbortMs', () => {
  it('derives the abort from the published editor wait plus the client margin', () => {
    const ms = deriveEditorAbortMs(
      caps({ editor_wait_seconds: 150, chat_wait_seconds: 330, text_wait_seconds: 90 }),
    )
    expect(ms).toBe(150 * 1000 + CLIENT_WAIT_MARGIN_MS)
  })

  it('tracks a raised server wait so the client is not silently capped', () => {
    const low = deriveEditorAbortMs(caps({ editor_wait_seconds: 150, chat_wait_seconds: 330, text_wait_seconds: 90 }))
    const high = deriveEditorAbortMs(caps({ editor_wait_seconds: 630, chat_wait_seconds: 1830, text_wait_seconds: 630 }))
    expect(high).toBeGreaterThan(low)
  })

  it('falls back when the backend exposes no timeouts block', () => {
    expect(deriveEditorAbortMs(caps())).toBe(EDITOR_FALLBACK_TIMEOUT_MS)
  })

  it('falls back when there is no manifest (offline / pre-discovery)', () => {
    expect(deriveEditorAbortMs(null)).toBe(EDITOR_FALLBACK_TIMEOUT_MS)
  })
})

describe('deriveChatStepTimeoutMs', () => {
  it('derives the abort from the published chat wait plus the client margin', () => {
    const ms = deriveChatStepTimeoutMs(
      caps({ editor_wait_seconds: 150, chat_wait_seconds: 330, text_wait_seconds: 90 }),
    )
    expect(ms).toBe(330 * 1000 + CLIENT_WAIT_MARGIN_MS)
  })

  it('falls back when the backend exposes no timeouts block', () => {
    expect(deriveChatStepTimeoutMs(caps())).toBe(CHAT_STEP_FALLBACK_TIMEOUT_MS)
  })

  it('falls back when there is no manifest', () => {
    expect(deriveChatStepTimeoutMs(null)).toBe(CHAT_STEP_FALLBACK_TIMEOUT_MS)
  })
})

describe('isMissingServerTimeouts', () => {
  it('is true only for a present manifest that omits the timeouts block', () => {
    expect(isMissingServerTimeouts(caps())).toBe(true)
  })

  it('is false when the timeouts block is present', () => {
    expect(
      isMissingServerTimeouts(caps({ editor_wait_seconds: 150, chat_wait_seconds: 330, text_wait_seconds: 90 })),
    ).toBe(false)
  })

  it('is false with no manifest (not an old-backend condition)', () => {
    expect(isMissingServerTimeouts(null)).toBe(false)
  })
})
