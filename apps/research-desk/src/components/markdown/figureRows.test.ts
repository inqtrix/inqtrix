import type { Root, RootContent } from 'mdast'
import { describe, expect, it } from 'vitest'

import { FIGURE_ROW_ATTRIBUTE, remarkFigureRows } from './figureRows'

const plot = (title: string): RootContent => ({
  lang: 'mermaid',
  type: 'code',
  value: `xychart-beta\n  title "${title}"\n  x-axis [a, b]\n  bar [1, 2]`,
})
const flow = (): RootContent => ({ lang: 'mermaid', type: 'code', value: 'flowchart LR\n  A --> B' })
const paragraph = (): RootContent => ({ children: [{ type: 'text', value: 'Text' }], type: 'paragraph' })

function transform(children: RootContent[]): Root {
  const tree: Root = { children, type: 'root' }
  const run = remarkFigureRows.call({} as never) as (tree: Root) => void
  run(tree)
  return tree
}

describe('remarkFigureRows', () => {
  it('pairs adjacent plots into one row node and leaves the fences untouched', () => {
    const tree = transform([paragraph(), plot('a'), plot('b'), paragraph()])
    expect(tree.children.map((node) => node.type)).toEqual(['paragraph', 'figureRow', 'paragraph'])
    const row = tree.children[1] as unknown as { children: RootContent[]; data: Record<string, unknown> }
    expect(row.children.map((node) => node.type)).toEqual(['code', 'code'])
    expect(row.data).toEqual({ hName: 'div', hProperties: { [FIGURE_ROW_ATTRIBUTE]: '' } })
  })

  it('never pairs a lone plot, diagrams, or plots separated by prose', () => {
    expect(transform([plot('a'), paragraph(), plot('b')]).children.map((node) => node.type))
      .toEqual(['code', 'paragraph', 'code'])
    expect(transform([flow(), flow()]).children.map((node) => node.type)).toEqual(['code', 'code'])
    expect(transform([plot('a'), flow(), plot('b')]).children.map((node) => node.type))
      .toEqual(['code', 'code', 'code'])
    expect(transform([plot('only')]).children.map((node) => node.type)).toEqual(['code'])
  })

  it('keeps three adjacent plots in one row', () => {
    const tree = transform([plot('a'), plot('b'), plot('c')])
    expect(tree.children).toHaveLength(1)
    expect((tree.children[0] as unknown as { children: unknown[] }).children).toHaveLength(3)
  })
})
