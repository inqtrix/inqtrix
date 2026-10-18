const MARKDOWN_LANGUAGE_ALIASES: Record<string, string> = {
  js: 'javascript',
  py: 'python',
  shell: 'shellscript',
  text: 'plaintext',
  ts: 'typescript',
}

export const MARKDOWN_COMMON_LANGUAGES = [
  'plaintext',
  'python',
  'bash',
  'sh',
  'shellscript',
  'json',
  'jsonc',
  'javascript',
  'typescript',
  'tsx',
  'jsx',
  'css',
  'html',
  'markdown',
] as const

const SUPPORTED_MARKDOWN_LANGUAGE_SET = new Set<string>(MARKDOWN_COMMON_LANGUAGES)

export function plainCodeLanguageFromClassName(className: unknown): string | null {
  if (Array.isArray(className)) {
    for (const item of className) {
      const language = plainCodeLanguageFromClassName(item)
      if (language) return language
    }
    return null
  }

  if (typeof className !== 'string') return null

  for (const token of className.split(/\s+/)) {
    if (!token.startsWith('language-')) continue
    const language = normalizeMarkdownCodeLanguage(token.slice('language-'.length))
    if (language) return language
  }

  return null
}

/** The RAW fence language token (lowercased) WITHOUT the Shiki
 * whitelist — for renderers that dispatch on languages the highlighter
 * does not know (the mermaid figure). `plainCodeLanguageFromClassName`
 * stays the highlighter-facing, whitelist-normalized reading. */
export function rawCodeLanguageFromClassName(className: unknown): string | null {
  if (Array.isArray(className)) {
    for (const item of className) {
      const language = rawCodeLanguageFromClassName(item)
      if (language) return language
    }
    return null
  }

  if (typeof className !== 'string') return null

  for (const token of className.split(/\s+/)) {
    if (!token.startsWith('language-')) continue
    const raw = token.slice('language-'.length).trim().toLowerCase()
    if (raw) return raw
  }

  return null
}

/** Alias-fold and whitelist one language token (`js`→`javascript`;
 * unknown → null). Shared by the chat renderer and the editor's code
 * blocks — the ONE vocabulary the highlighter understands. */
export function normalizeMarkdownCodeLanguage(value: unknown): string | null {
  if (typeof value !== 'string') return null

  const token = value
    .trim()
    .split(/\s+/)[0]
    ?.replace(/^language-/i, '')
    .toLowerCase()

  if (!token) return null

  const normalized = MARKDOWN_LANGUAGE_ALIASES[token] ?? token
  return SUPPORTED_MARKDOWN_LANGUAGE_SET.has(normalized) ? normalized : null
}
