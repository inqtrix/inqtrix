/**
 * Marks the blocks of a rendered Markdown answer whose source lines a review
 * changed (`features/agent/answerDraft.ts` names the lines).
 *
 * The answer renders as ONE document, so lists keep their numbering and
 * tables their header; the mark sits on the innermost block that holds a
 * changed line: the paragraph of a loose list item, the item of a tight
 * list, the table row, the heading, the code block. A marked block carries
 * `data-review-changed`, styled in `globals.css`.
 */

type ReviewMarkHastNode = {
  children?: ReviewMarkHastNode[]
  position?: { end?: { line?: number }; start?: { line?: number } }
  properties?: Record<string, unknown>
  tagName?: string
  type: string
}

const MARKABLE_TAGS = new Set([
  'blockquote', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'li', 'p', 'pre', 'tr',
])

/** Code languages whose renderer replaces the whole `pre` (KaTeX for a
 * display formula, the diagram figure for Mermaid): their mark sits on a
 * block around the `pre`, which the replacement leaves in place. */
const REPLACED_CODE_CLASSES = ['language-math', 'language-mermaid']

/** Rehype plugin; it reads the source positions the Markdown parser left. */
export function rehypeReviewMarks(options: { changedLines: ReadonlySet<number> }) {
  return (tree: ReviewMarkHastNode) => {
    markChangedBlocks(tree.children ?? [], options.changedLines)
  }
}

/** Marks the innermost markable blocks over a changed line; returns whether
 * any block among *nodes* or below them was marked. */
function markChangedBlocks(
  nodes: ReviewMarkHastNode[],
  changedLines: ReadonlySet<number>,
): boolean {
  let marked = false
  nodes.forEach((node, index) => {
    if (node.type !== 'element') return
    if (markChangedBlocks(node.children ?? [], changedLines)) {
      marked = true
      return
    }
    if (!node.tagName || !MARKABLE_TAGS.has(node.tagName) || !holdsChangedLine(node, changedLines)) {
      return
    }
    marked = true
    if (isReplacedCode(node)) {
      nodes[index] = {
        children: [node],
        position: node.position,
        properties: { dataReviewChanged: '' },
        tagName: 'div',
        type: 'element',
      }
      return
    }
    node.properties = { ...node.properties, dataReviewChanged: '' }
  })
  return marked
}

function isReplacedCode(node: ReviewMarkHastNode): boolean {
  if (node.tagName !== 'pre') return false
  const code = node.children?.find((child) => child.type === 'element' && child.tagName === 'code')
  const classes = code?.properties?.className
  return Array.isArray(classes)
    && classes.some((name) => REPLACED_CODE_CLASSES.includes(String(name)))
}

function holdsChangedLine(node: ReviewMarkHastNode, changedLines: ReadonlySet<number>): boolean {
  const start = node.position?.start?.line
  const end = node.position?.end?.line
  if (start === undefined || end === undefined) return false
  for (let line = start; line <= end; line += 1) {
    if (changedLines.has(line)) return true
  }
  return false
}
