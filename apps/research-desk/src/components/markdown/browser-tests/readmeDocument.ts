import type { Nodes } from 'mdast'
import remarkParse from 'remark-parse'
import { unified } from 'unified'

const parser = unified().use(remarkParse)

function textContent(node: Nodes): string {
  if (node.type === 'text' || node.type === 'inlineCode') return node.value
  return 'children' in node ? node.children.map(textContent).join('') : ''
}

/** Use the repository's actual fence, including its original delimiters. */
export function readmeArchitectureMarkdown(markdown: string): string {
  let inArchitecture = false
  const diagrams = []
  for (const node of parser.parse(markdown).children) {
    if (node.type === 'heading' && node.depth === 2) {
      inArchitecture = textContent(node) === 'Architecture & components'
    } else if (inArchitecture && node.type === 'code' && node.lang === 'mermaid') {
      diagrams.push(node)
    }
  }
  if (diagrams.length !== 1) throw new Error('README architecture must contain exactly one Mermaid diagram.')
  const start = diagrams[0].position?.start.offset
  const end = diagrams[0].position?.end.offset
  if (start === undefined || end === undefined) throw new Error('README diagram has no source positions.')
  return markdown.slice(start, end)
}

/** Inspect Markdown links and the quoted HTML attributes used by the README.
 * Parsing blocks first keeps examples inside code out of the navigation. */
export function readmeNavigation(markdown: string): { anchors: Set<string>; links: string[] } {
  const anchors = new Set<string>()
  const links: string[] = []
  const headingCounts = new Map<string, number>()

  function visit(node: Nodes): void {
    if (node.type === 'heading') {
      const base = textContent(node).toLowerCase()
        .replace(/[^\p{L}\p{N}\p{M}_\-\s]/gu, '').replace(/\s/g, '-')
      const occurrence = headingCounts.get(base) ?? 0
      headingCounts.set(base, occurrence + 1)
      anchors.add(occurrence ? `${base}-${occurrence}` : base)
    } else if (node.type === 'link' || node.type === 'image') {
      links.push(node.url)
    } else if (node.type === 'html') {
      const html = node.value.replace(/<!--[\s\S]*?-->/g, '')
      for (const tag of html.matchAll(/<([a-z][\w:-]*)\b([^>]*)>/gi)) {
        for (const attribute of tag[2].matchAll(/(?:^|\s)(id|name|href|src)\s*=\s*(?:"([^"]*)"|'([^']*)')/gi)) {
          const kind = attribute[1].toLowerCase()
          const value = attribute[2] ?? attribute[3]
          if (kind === 'id' || (kind === 'name' && tag[1].toLowerCase() === 'a')) anchors.add(value)
          if (kind === 'href' || kind === 'src') links.push(value)
        }
      }
    }
    if ('children' in node) node.children.forEach(visit)
  }

  visit(parser.parse(markdown))
  return { anchors, links }
}
