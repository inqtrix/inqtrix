type MathMlHastNode = {
  children?: MathMlHastNode[]
  properties?: Record<string, unknown>
  tagName?: string
  type: string
  value?: string
}

const SCRIPT_ARITY: Readonly<Record<string, { expected: number; scriptIndex: number }>> = {
  mover: { expected: 2, scriptIndex: 1 },
  msub: { expected: 2, scriptIndex: 1 },
  msubsup: { expected: 3, scriptIndex: 1 },
  munderover: { expected: 3, scriptIndex: 1 },
  munder: { expected: 2, scriptIndex: 1 },
  msup: { expected: 2, scriptIndex: 1 },
}

/** KaTeX represents an operator used as a script with an invisible function-
 * application `<mo>`. Its MathML output places that node directly inside
 * `msub`/`msup` after the permitted children. Browsers that validate MathML
 * report the invalid arity. The operator belongs to the script expression, so
 * fold it into a script `mrow` without changing the visible HTML rendering. */
export function normalizeMathMlScriptArity(node: MathMlHastNode): void {
  if (node.type === 'element' && node.tagName && node.children) {
    const contract = SCRIPT_ARITY[node.tagName]
    if (
      contract
      && node.children.length > contract.expected
      && node.children.slice(contract.expected).every(isFunctionApplication)
    ) {
      const required = node.children.slice(0, contract.expected)
      const script = required[contract.scriptIndex]
      const extras = node.children.slice(contract.expected)
      required[contract.scriptIndex] = {
        children: [script, ...extras],
        properties: {},
        tagName: 'mrow',
        type: 'element',
      }
      node.children = required
    }
  }

  for (const child of node.children ?? []) normalizeMathMlScriptArity(child)
}

/** Rehype plugin applied after rehype-katex. */
export function rehypeNormalizeMathMlScripts() {
  return (tree: MathMlHastNode) => normalizeMathMlScriptArity(tree)
}

function isFunctionApplication(node: MathMlHastNode): boolean {
  return node.type === 'element'
    && node.tagName === 'mo'
    && node.children?.length === 1
    && node.children[0]?.type === 'text'
    && node.children[0].value === '\u2061'
}
