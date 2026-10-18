import { describe, expect, it } from 'vitest'

import { normalizeMathMlScriptArity } from './mathMl'

type TestNode = TestElement | { type: 'text'; value: string }
type TestElement = {
  children: TestNode[]
  properties: Record<string, unknown>
  tagName: string
  type: 'element'
}

const text = (value: string): TestNode => ({ type: 'text', value })
const element = (tagName: string, children: TestNode[] = []): TestElement => ({
  children,
  properties: {},
  tagName,
  type: 'element',
})

function asElement(node: TestNode): TestElement {
  if (node.type !== 'element') throw new Error('expected an element node')
  return node
}

describe('normalizeMathMlScriptArity', () => {
  it('folds KaTeX function application into a two-child subscript', () => {
    const base = element('mi', [text('V')])
    const script = element('mi', [text('min')])
    const apply = element('mo', [text('\u2061')])
    const tree = element('math', [element('msub', [base, script, apply])])

    normalizeMathMlScriptArity(tree)

    const subscript = asElement(tree.children[0])
    expect(subscript.children).toHaveLength(2)
    expect(subscript.children[1]).toEqual(element('mrow', [script, apply]))
  })

  it('preserves the superscript while repairing msubsup', () => {
    const base = element('mi', [text('V')])
    const script = element('mi', [text('min')])
    const superscript = element('mn', [text('2')])
    const apply = element('mo', [text('\u2061')])
    const tree = element('math', [element('msubsup', [base, script, superscript, apply])])

    normalizeMathMlScriptArity(tree)

    const scripted = asElement(tree.children[0])
    expect(scripted.children).toHaveLength(3)
    expect(scripted.children[1]).toEqual(element('mrow', [script, apply]))
    expect(scripted.children[2]).toBe(superscript)
  })

  it('does not alter already valid script markup or unrelated extra nodes', () => {
    const valid = element('msub', [element('mi'), element('mn')])
    const unrelatedExtra = element('msub', [element('mi'), element('mn'), element('mi')])
    const tree = element('math', [valid, unrelatedExtra])

    normalizeMathMlScriptArity(tree)

    expect(valid.children).toHaveLength(2)
    expect(unrelatedExtra.children).toHaveLength(3)
  })
})
