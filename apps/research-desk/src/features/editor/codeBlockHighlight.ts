/**
 * Editor code-block syntax highlighting — display-only decorations
 * over the SAME shiki core, token cache, and language vocabulary as the
 * chat renderer. Deliberately NOT a code-block extension change: the
 * schema (StarterKit codeBlock incl. its suggestion marks and the
 * `language` attribute) stays byte-identical; everything here is an
 * app-side ProseMirror plugin the collaboration fingerprint never sees.
 *
 * Async shape: a build pass reads the token cache synchronously and
 * SCHEDULES misses; when a scheduled highlight lands, the plugin view
 * dispatches a refresh-meta transaction and the next build finds the
 * tokens in the cache. Failures inside the shared core degrade to the
 * unhighlighted block (visible as plain text, warned in the console) —
 * never a broken editor.
 */

import { Extension } from '@tiptap/core'
import { type Node as ProseMirrorNode } from '@tiptap/pm/model'
import { Plugin, PluginKey } from '@tiptap/pm/state'
import { Decoration, DecorationSet } from '@tiptap/pm/view'

import {
  ensureMarkdownCodeHighlight,
  markdownCodeCacheKey,
  markdownCodeTheme,
  markdownTokenCache,
  markdownTokenStyle,
  shikiCodeLanguage,
  type MarkdownCodeTheme,
  type MarkdownHighlightedLine,
} from '@/components/markdown/codeHighlight'
import { normalizeMarkdownCodeLanguage } from '@/components/markdown/markdownLanguage'

const codeBlockHighlightKey = new PluginKey<DecorationSet>(
  'codeBlockHighlight',
)
const REFRESH_META = 'codeBlockHighlight$refresh'

export type CodeBlockHighlightJob = {
  code: string
  language: string
  theme: MarkdownCodeTheme
}

/** The effective highlighter language of one code block: alias-folded,
 * whitelisted, unknown values fall back to plaintext — the block KEEPS
 * its foreign `language` attribute untouched (display-only fallback,
 * same behavior as the chat renderer). */
export function codeBlockHighlightLanguage(raw: unknown): string {
  return shikiCodeLanguage(
    normalizeMarkdownCodeLanguage(raw) ?? 'text',
  )
}

/**
 * Pure build pass: cache hits become inline token decorations, misses
 * are handed to `schedule` (deduped by cache key by the caller's map).
 * Exported for tests — the offset math (content starts at `pos + 1`,
 * one position per newline between lines) is the part worth pinning.
 */
export function buildCodeBlockHighlightDecorations(
  doc: ProseMirrorNode,
  theme: MarkdownCodeTheme,
  readLines: (cacheKey: string) => MarkdownHighlightedLine[] | undefined,
  schedule: (cacheKey: string, job: CodeBlockHighlightJob) => void,
): DecorationSet {
  const decorations: Decoration[] = []
  doc.descendants((node, pos) => {
    if (node.type.name !== 'codeBlock') return undefined
    const code = node.textContent
    if (!code) return false
    const language = codeBlockHighlightLanguage(node.attrs.language)
    const cacheKey = markdownCodeCacheKey(code, language, theme)
    const lines = readLines(cacheKey)
    if (lines === undefined) {
      schedule(cacheKey, { code, language, theme })
      return false
    }
    let offset = pos + 1
    for (const [lineIndex, line] of lines.entries()) {
      for (const token of line) {
        const from = offset
        offset += token.content.length
        const style = markdownTokenStyle(token)
        if (!style) continue
        const parts: string[] = []
        if (style.color) parts.push(`color:${style.color}`)
        if (style.fontStyle) parts.push(`font-style:${style.fontStyle}`)
        if (style.fontWeight) parts.push(`font-weight:${style.fontWeight}`)
        if (style.textDecoration) {
          parts.push(`text-decoration:${style.textDecoration}`)
        }
        if (parts.length === 0) continue
        decorations.push(
          Decoration.inline(from, offset, { style: parts.join(';') }),
        )
      }
      if (lineIndex < lines.length - 1) offset += 1
    }
    return false
  })
  return DecorationSet.create(doc, decorations)
}

function currentEditorCodeTheme(): MarkdownCodeTheme {
  const dark =
    typeof document !== 'undefined'
    && document.documentElement.classList.contains('dark')
  return markdownCodeTheme(dark ? 'dark' : 'light')
}

export const CodeBlockHighlightExtension = Extension.create({
  name: 'codeBlockHighlight',

  addProseMirrorPlugins() {
    // Jobs collected during a build pass; the plugin VIEW owns their
    // execution so a dispatch can only reach a live editor view.
    const pendingJobs = new Map<string, CodeBlockHighlightJob>()
    let drain: (() => void) | null = null

    const build = (doc: ProseMirrorNode): DecorationSet => {
      const decorations = buildCodeBlockHighlightDecorations(
        doc,
        currentEditorCodeTheme(),
        (cacheKey) => markdownTokenCache.get(cacheKey),
        (cacheKey, job) => {
          if (!pendingJobs.has(cacheKey)) pendingJobs.set(cacheKey, job)
        },
      )
      if (pendingJobs.size > 0) queueMicrotask(() => drain?.())
      return decorations
    }

    return [
      new Plugin({
        key: codeBlockHighlightKey,
        state: {
          init: (_config, state) => build(state.doc),
          apply: (tr, decorations) => {
            if (tr.docChanged || tr.getMeta(REFRESH_META) === true) {
              return build(tr.doc)
            }
            return decorations.map(tr.mapping, tr.doc)
          },
        },
        props: {
          decorations(state) {
            return codeBlockHighlightKey.getState(state)
          },
        },
        view: (view) => {
          let destroyed = false
          let refreshQueued = false
          const refresh = () => {
            if (destroyed || refreshQueued) return
            refreshQueued = true
            requestAnimationFrame(() => {
              refreshQueued = false
              if (destroyed) return
              view.dispatch(view.state.tr.setMeta(REFRESH_META, true))
            })
          }
          drain = () => {
            if (destroyed || pendingJobs.size === 0) return
            const jobs = [...pendingJobs.values()]
            pendingJobs.clear()
            for (const job of jobs) {
              // The shared core dedupes in-flight work and caches the
              // result; every settled job re-reads through one refresh.
              void ensureMarkdownCodeHighlight(job).then(refresh)
            }
          }
          // The editor instance survives theme toggles (legacy mode never
          // rebuilds it) — watch the root's class list like the chat's
          // useTheme() would and re-key the cache lookups.
          const observer = typeof MutationObserver !== 'undefined'
            ? new MutationObserver(refresh)
            : null
          observer?.observe(document.documentElement, {
            attributeFilter: ['class'],
            attributes: true,
          })
          return {
            destroy: () => {
              destroyed = true
              drain = null
              observer?.disconnect()
            },
          }
        },
      }),
    ]
  },
})
