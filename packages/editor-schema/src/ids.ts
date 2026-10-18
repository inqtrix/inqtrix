export type SecureUuidCrypto = Pick<Crypto, 'getRandomValues'>
  & Partial<Pick<Crypto, 'randomUUID'>>

/**
 * Create an RFC 4122 version 4 identifier without requiring a secure context.
 *
 * Browsers expose `getRandomValues` in HTTP LAN contexts where `randomUUID`
 * is intentionally unavailable. Failing loudly when neither secure primitive
 * exists prevents identifiers from degrading to predictable randomness.
 */
export function createSecureUuid(
  cryptoApi: SecureUuidCrypto | null | undefined = globalThis.crypto,
): string {
  if (typeof cryptoApi?.randomUUID === 'function') {
    return cryptoApi.randomUUID()
  }
  if (typeof cryptoApi?.getRandomValues !== 'function') {
    throw new Error('A secure random number generator is unavailable.')
  }

  const bytes = cryptoApi.getRandomValues(new Uint8Array(16))
  bytes[6] = ((bytes[6] ?? 0) & 0x0f) | 0x40
  bytes[8] = ((bytes[8] ?? 0) & 0x3f) | 0x80
  const hex = bytesToHex(bytes)
  return [
    hex.slice(0, 8),
    hex.slice(8, 12),
    hex.slice(12, 16),
    hex.slice(16, 20),
    hex.slice(20),
  ].join('-')
}

export function createSecurePrefixedId(prefix: string): string {
  return `${prefix}-${createSecureUuid()}`
}

function bytesToHex(bytes: Uint8Array): string {
  return [...bytes]
    .map((value) => value.toString(16).padStart(2, '0'))
    .join('')
}
