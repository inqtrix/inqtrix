import type { ReactNode } from 'react'

/**
 * Inline Markdown for ONE transcript line: bold, italic, code and links —
 * nothing block-level. Narration lines are derived from Markdown the
 * agent wrote (a report head, a plan summary), so their emphasis has to
 * render instead of showing `**` to the reader; leading heading markers
 * are dropped. Unbalanced markers stay literal (a line that is still
 * typing in must never swallow text).
 */
const TOKEN = /(\*\*[^*]+\*\*|`[^`]+`|\[[^\]]+\]\((?:https?:\/\/|\/)[^)\s]+\)|(?<![\w*])\*[^*\n]+\*(?![\w*])|(?<![\w_])_[^_\n]+_(?![\w_]))/

export function stripHeadingMarkers(text: string): string {
  return text.replace(/(^|\n)#{1,6}[ \t]+/g, '$1')
}

export function renderInlineMarkdown(text: string): ReactNode[] {
  const nodes: ReactNode[] = []
  const parts = stripHeadingMarkers(text).split(TOKEN)
  parts.forEach((part, index) => {
    if (!part) return
    const key = `${index}-${part.slice(0, 12)}`
    if (part.startsWith('**') && part.endsWith('**') && part.length > 4) {
      nodes.push(<strong className="font-semibold text-foreground" key={key}>{part.slice(2, -2)}</strong>)
      return
    }
    if (part.startsWith('`') && part.endsWith('`') && part.length > 2) {
      nodes.push(<code className="rounded bg-muted px-1 font-mono text-[0.85em]" key={key}>{part.slice(1, -1)}</code>)
      return
    }
    const link = /^\[([^\]]+)\]\(([^)\s]+)\)$/.exec(part)
    if (link) {
      nodes.push(
        <a className="underline decoration-muted-foreground/50 underline-offset-2 hover:text-foreground" href={link[2]} key={key} rel="noreferrer" target="_blank">
          {link[1]}
        </a>,
      )
      return
    }
    if ((part.startsWith('*') && part.endsWith('*')) || (part.startsWith('_') && part.endsWith('_'))) {
      if (part.length > 2) {
        nodes.push(<em key={key}>{part.slice(1, -1)}</em>)
        return
      }
    }
    nodes.push(part)
  })
  return nodes
}
