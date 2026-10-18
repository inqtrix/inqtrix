export type MarkdownImageSourcePolicy =
  | { kind: 'direct'; src: string }
  | { host: string; kind: 'external'; src: string }
  | { kind: 'invalid' }

export function classifyMarkdownImageSource(
  source: string | undefined,
  baseHref: string,
): MarkdownImageSourcePolicy {
  const src = source?.trim()
  if (!src) return { kind: 'invalid' }

  try {
    const base = new URL(baseHref)
    const resolved = new URL(src, base)
    if (resolved.protocol !== 'http:' && resolved.protocol !== 'https:') {
      return { kind: 'invalid' }
    }
    if (resolved.origin !== base.origin) {
      return { host: resolved.host, kind: 'external', src: resolved.href }
    }
    return { kind: 'direct', src }
  } catch {
    return { kind: 'invalid' }
  }
}
