import { unified } from 'unified'
import remarkGfm from 'remark-gfm'
import remarkMath from 'remark-math'
import remarkParse from 'remark-parse'

type MarkdownNode = {
  alt?: unknown
  children?: MarkdownNode[]
  position?: {
    end?: { offset?: number }
    start?: { offset?: number }
  }
  type: string
  value?: unknown
}

type SelectionCandidate = {
  end: number
  markdown: string
  plainText: string
  start: number
}

const markdownParser = unified()
  .use(remarkParse)
  .use(remarkGfm)
  .use(remarkMath)

export function normalizeSelectionText(value: string): string {
  return value
    .replace(/\u00a0/g, ' ')
    .replace(/\r\n?/g, '\n')
    .replace(/[ \t]+\n/g, '\n')
    .replace(/\n[ \t]+/g, '\n')
    .replace(/[ \t]+/g, ' ')
    .replace(/\n{2,}/g, '\n')
    .trim()
}

export function markdownForVisibleSelection(
  markdown: string,
  selectedText: string,
): string | null {
  const normalizedSelection = normalizeSelectionText(selectedText)
  if (!normalizedSelection) return null

  let root: MarkdownNode
  try {
    root = markdownParser.parse(markdown) as MarkdownNode
  } catch {
    return null
  }

  const candidates: SelectionCandidate[] = [{
    end: markdown.length,
    markdown: markdown.trim(),
    plainText: visibleTextForNode(root),
    start: 0,
  }]
  collectCandidates(root, markdown, candidates)

  const matches = dedupeCandidates(candidates)
    .filter((candidate) => normalizeSelectionText(candidate.plainText) === normalizedSelection)
  if (matches.length === 0) return null

  const maxScore = Math.max(...matches.map((candidate) => syntaxScore(candidate)))
  const best = matches.filter((candidate) => syntaxScore(candidate) === maxScore)
  if (best.length !== 1) return null
  return best[0].markdown
}

function collectCandidates(
  node: MarkdownNode,
  markdown: string,
  candidates: SelectionCandidate[],
) {
  const source = sourceForNode(markdown, node)
  const plainText = visibleTextForNode(node)
  if (source && normalizeSelectionText(plainText)) {
    candidates.push({
      end: source.end,
      markdown: source.markdown.trim(),
      plainText,
      start: source.start,
    })
  }

  for (const child of node.children ?? []) {
    collectCandidates(child, markdown, candidates)
  }
}

function sourceForNode(markdown: string, node: MarkdownNode): { end: number; markdown: string; start: number } | null {
  const start = node.position?.start?.offset
  const end = node.position?.end?.offset
  if (typeof start !== 'number' || typeof end !== 'number' || start < 0 || end <= start) {
    return null
  }
  return { end, markdown: markdown.slice(start, end), start }
}

function dedupeCandidates(candidates: SelectionCandidate[]): SelectionCandidate[] {
  const seen = new Set<string>()
  const unique: SelectionCandidate[] = []
  for (const candidate of candidates) {
    const key = `${candidate.start}:${candidate.end}:${candidate.markdown}`
    if (seen.has(key)) continue
    seen.add(key)
    unique.push(candidate)
  }
  return unique
}

function syntaxScore(candidate: SelectionCandidate): number {
  return Math.max(0, candidate.markdown.length - candidate.plainText.trim().length)
}

function visibleTextForNode(node: MarkdownNode): string {
  switch (node.type) {
    case 'root':
    case 'blockquote':
    case 'list':
      return joinChildren(node, '\n')
    case 'listItem':
      return joinChildren(node, '\n')
    case 'paragraph':
    case 'heading':
    case 'emphasis':
    case 'strong':
    case 'delete':
    case 'link':
    case 'linkReference':
      return joinChildren(node, '')
    case 'table':
      return joinChildren(node, '\n')
    case 'tableRow':
      return joinChildren(node, '\t')
    case 'tableCell':
      return joinChildren(node, ' ')
    case 'break':
      return '\n'
    case 'code':
    case 'html':
    case 'inlineCode':
    case 'inlineMath':
    case 'math':
    case 'text':
      return typeof node.value === 'string' ? node.value : ''
    case 'image':
    case 'imageReference':
      return typeof node.alt === 'string' ? node.alt : ''
    default:
      return joinChildren(node, '\n')
  }
}

function joinChildren(node: MarkdownNode, separator: string): string {
  const children = node.children ?? []
  if (separator === '') {
    return children.map(visibleTextForNode).join('')
  }
  return children
    .map(visibleTextForNode)
    .filter((text) => normalizeSelectionText(text) !== '')
    .join(separator)
}
