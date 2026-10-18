import { describe, expect, it, vi } from 'vitest'

import { createSecurePrefixedId, createSecureUuid } from '../src/ids.js'

describe('secure editor identifiers', () => {
  it('uses native randomUUID when the browser exposes it', () => {
    const randomUUID = vi.fn(
      () => '11111111-2222-4333-8444-555555555555' as `${string}-${string}-${string}-${string}-${string}`,
    )
    const getRandomValues = vi.fn()

    expect(createSecureUuid({
      getRandomValues: getRandomValues as Crypto['getRandomValues'],
      randomUUID,
    })).toBe('11111111-2222-4333-8444-555555555555')
    expect(getRandomValues).not.toHaveBeenCalled()
  })

  it('creates RFC 4122 v4 identifiers over LAN HTTP with getRandomValues only', () => {
    const getRandomValues = vi.fn((bytes: Uint8Array) => {
      bytes.set([
        0x00, 0x11, 0x22, 0x33,
        0x44, 0x55,
        0x66, 0x77,
        0x08, 0x99,
        0xaa, 0xbb, 0xcc, 0xdd, 0xee, 0xff,
      ])
      return bytes
    })
    const cryptoApi = {
      getRandomValues: getRandomValues as Crypto['getRandomValues'],
    }

    expect(createSecureUuid(cryptoApi)).toBe('00112233-4455-4677-8899-aabbccddeeff')
    expect(createSecurePrefixedId('patch')).toMatch(/^patch-[0-9a-f-]{36}$/)
  })

  it('fails loudly instead of falling back to predictable randomness', () => {
    expect(() => createSecureUuid(null)).toThrow(
      'A secure random number generator is unavailable.',
    )
  })
})
