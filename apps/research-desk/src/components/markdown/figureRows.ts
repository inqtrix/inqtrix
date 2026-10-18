import { createContext, useContext } from 'react'
import type { Code, Root, RootContent } from 'mdast'
import type { Plugin } from 'unified'

import { detectMermaidDiagramType, type MermaidLayout } from './mermaidTheme'

/**
 * Two data plots written directly after each other share one row, like
 * the paired sub-figures of a paper. The author needs no extra syntax —
 * adjacency is the signal — and only plots pair up: a flowchart followed
 * by a sequence diagram stays stacked. On a narrow column the row wraps.
 */
export const FIGURE_ROW_ATTRIBUTE = 'data-figure-row'

/** Grammars that pair up when adjacent. */
export const MERMAID_ROW_DIAGRAMS: readonly string[] = ['xychart-beta']

/** What a figure inside the current markdown subtree should size itself for. */
export const MermaidLayoutContext = createContext<MermaidLayout>('single')

export function useMermaidLayout(): MermaidLayout {
  return useContext(MermaidLayoutContext)
}

function isRowPlot(node: RootContent): node is Code {
  return node.type === 'code'
    && (node.lang ?? '').toLowerCase() === 'mermaid'
    && MERMAID_ROW_DIAGRAMS.includes(detectMermaidDiagramType(node.value) ?? '')
}

/** Groups runs of two or more adjacent plot fences into a `figureRow` node
 * that renders as a `<div data-figure-row>` around the untouched fences. */
export const remarkFigureRows: Plugin<[], Root> = () => (tree) => {
  const children: RootContent[] = []
  let run: Code[] = []
  const flush = () => {
    if (run.length >= 2) {
      children.push({
        children: run,
        data: { hName: 'div', hProperties: { [FIGURE_ROW_ATTRIBUTE]: '' } },
        type: 'figureRow',
      } as unknown as RootContent)
    } else {
      children.push(...run)
    }
    run = []
  }
  for (const node of tree.children) {
    if (isRowPlot(node)) {
      run.push(node)
    } else {
      flush()
      children.push(node)
    }
  }
  flush()
  tree.children = children
}
