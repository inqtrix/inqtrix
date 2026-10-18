/**
 * THE shiki code-highlighting core, shared by every surface (chat/report
 * markdown AND the editor's code blocks). Extracted verbatim from
 * MarkdownRenderer.tsx — one lazy singleton highlighter, one module-global
 * token LRU keyed by `theme\0lang\0code`, fire-and-forget population.
 * Consumers subscribe to the cache (`useMarkdownCacheEntry`) or re-read it
 * after awaiting {@link ensureMarkdownCodeHighlight}.
 */

import type { CSSProperties } from 'react'
import type { Highlighter, ThemedToken } from 'shiki'
import { createBundledHighlighter } from 'shiki/core'
import { createJavaScriptRegexEngine } from 'shiki/engine/javascript'

import { BoundedLruCache, MARKDOWN_RENDER_CACHE_CAPACITY } from './boundedLruCache'

const createMarkdownHighlighter = createBundledHighlighter({
  engine: () => createJavaScriptRegexEngine(),
  langs: {
    bash: () => import('shiki/dist/langs/shellscript.mjs'),
    css: () => import('shiki/dist/langs/css.mjs'),
    html: () => import('shiki/dist/langs/html.mjs'),
    javascript: () => import('shiki/dist/langs/javascript.mjs'),
    json: () => import('shiki/dist/langs/json.mjs'),
    jsonc: () => import('shiki/dist/langs/jsonc.mjs'),
    jsx: () => import('shiki/dist/langs/jsx.mjs'),
    markdown: () => import('shiki/dist/langs/markdown.mjs'),
    python: () => import('shiki/dist/langs/python.mjs'),
    sh: () => import('shiki/dist/langs/shellscript.mjs'),
    shellscript: () => import('shiki/dist/langs/shellscript.mjs'),
    tsx: () => import('shiki/dist/langs/tsx.mjs'),
    typescript: () => import('shiki/dist/langs/typescript.mjs'),
  },
  themes: {
    'github-dark': () => import('shiki/dist/themes/github-dark.mjs'),
    'github-light': () => import('shiki/dist/themes/github-light.mjs'),
  },
})

type MarkdownHighlighterOptions = Parameters<typeof createMarkdownHighlighter>[0]
type MarkdownLoadLanguage = Parameters<Highlighter['loadLanguage']>[0]
type MarkdownTokenizeLanguage = NonNullable<
  Parameters<Highlighter['codeToTokens']>[1]['lang']
>
export type MarkdownCodeTheme = 'github-dark' | 'github-light'
export type MarkdownHighlightedLine = Array<
  Pick<ThemedToken, 'color' | 'content' | 'fontStyle'>
>

let markdownHighlighterPromise: Promise<Highlighter> | null = null
export const markdownTokenCache = new BoundedLruCache<
  string,
  MarkdownHighlightedLine[]
>(MARKDOWN_RENDER_CACHE_CAPACITY)
const markdownTokenPending = new Set<string>()

function getMarkdownHighlighter(
  options: MarkdownHighlighterOptions,
): Promise<Highlighter> {
  markdownHighlighterPromise ??= createMarkdownHighlighter(options)
    .then((highlighter) => highlighter as unknown as Highlighter)
  return markdownHighlighterPromise
}

export function markdownCodeTheme(
  resolvedTheme: 'light' | 'dark',
): MarkdownCodeTheme {
  return resolvedTheme === 'dark' ? 'github-dark' : 'github-light'
}

export function shikiCodeLanguage(language: string): string {
  return language === 'text' ? 'plaintext' : language
}

export function markdownCodeCacheKey(
  code: string,
  language: string,
  theme: MarkdownCodeTheme,
): string {
  return `${theme}\u0000${language}\u0000${code}`
}

export async function ensureMarkdownCodeHighlight({
  code,
  language,
  theme,
}: {
  code: string
  language: string
  theme: MarkdownCodeTheme
}): Promise<void> {
  const normalizedLanguage = shikiCodeLanguage(language)
  const cacheKey = markdownCodeCacheKey(code, normalizedLanguage, theme)
  if (markdownTokenCache.has(cacheKey) || markdownTokenPending.has(cacheKey)) return

  markdownTokenPending.add(cacheKey)
  try {
    const highlighter = await getMarkdownHighlighter({
      langs: ['plaintext'],
      themes: ['github-dark', 'github-light'],
    })
    if (normalizedLanguage !== 'plaintext') {
      await highlighter.loadLanguage(normalizedLanguage as MarkdownLoadLanguage)
    }
    const result = highlighter.codeToTokens(code, {
      lang: normalizedLanguage as MarkdownTokenizeLanguage,
      theme,
      tokenizeMaxLineLength: 900,
      tokenizeTimeLimit: 200,
    })
    markdownTokenCache.set(
      cacheKey,
      result.tokens.map((line) =>
        line.map((token) => ({
          color: token.color,
          content: token.content,
          fontStyle: token.fontStyle,
        })),
      ),
    )
  } catch (error) {
    console.warn('Inqtrix markdown code highlight failed.', error)
  } finally {
    markdownTokenPending.delete(cacheKey)
  }
}

export function markdownTokenStyle(
  token: Pick<ThemedToken, 'color' | 'fontStyle'>,
): CSSProperties | undefined {
  const style: CSSProperties = {}
  if (token.color) style.color = token.color

  if (typeof token.fontStyle === 'number' && token.fontStyle > 0) {
    if (token.fontStyle & 1) style.fontStyle = 'italic'
    if (token.fontStyle & 2) style.fontWeight = 700
    if (token.fontStyle & 4) style.textDecoration = 'underline'
  }

  return Object.keys(style).length > 0 ? style : undefined
}
