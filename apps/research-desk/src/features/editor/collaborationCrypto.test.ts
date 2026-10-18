import { describe, expect, it, vi } from 'vitest'

import {
  collaborationCommandId,
  collaborationSha256Hex,
} from './collaborationCrypto'

describe('collaboration crypto fallbacks', () => {
  it.each([
    ['', 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'],
    ['abc', 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad'],
    [
      'Kollaboration 🔐',
      '2e60aefc6f6afc8edec6a125cfa76faa83cf39002d3de41c2440babd2524952d',
    ],
    [
      'abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq',
      '248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1',
    ],
  ])('hashes %j without SubtleCrypto', async (value, expected) => {
    await expect(
      collaborationSha256Hex(new TextEncoder().encode(value), null),
    ).resolves.toBe(expected)
  })

  it('prefers the browser SubtleCrypto implementation when available', async () => {
    const digest = Uint8Array.from({ length: 32 }, (_, index) => index).buffer
    const subtle = {
      digest: vi.fn().mockResolvedValue(digest),
    } as unknown as SubtleCrypto

    await expect(
      collaborationSha256Hex(Uint8Array.from([1, 2, 3]), subtle),
    ).resolves.toBe(
      '000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f',
    )
    expect(subtle.digest).toHaveBeenCalledWith('SHA-256', expect.any(ArrayBuffer))
  })

  it('creates an RFC 4122 version 4 id when randomUUID is unavailable', () => {
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

    expect(collaborationCommandId({
      getRandomValues: getRandomValues as Crypto['getRandomValues'],
    })).toBe('00112233-4455-4677-8899-aabbccddeeff')
    expect(getRandomValues).toHaveBeenCalledOnce()
  })

  it('prefers native randomUUID when available', () => {
    const randomUUID = vi.fn(
      () => '11111111-2222-4333-8444-555555555555' as ReturnType<Crypto['randomUUID']>,
    )
    const getRandomValues = vi.fn()

    expect(collaborationCommandId({
      getRandomValues: getRandomValues as Crypto['getRandomValues'],
      randomUUID,
    })).toBe('11111111-2222-4333-8444-555555555555')
    expect(getRandomValues).not.toHaveBeenCalled()
  })
})
