import { readFileSync } from 'node:fs'
import path from 'node:path'

import ts from 'typescript'
import { describe, expect, it } from 'vitest'

// Source pins: the shell is not rendered in this lane, and neither the type
// checker nor the linter sees these two gaps. A preference missing from the
// memo's dependencies is pushed only when some other preference changes; a
// preference the account row never applies keeps the device value after
// sign-in, and the next save overwrites the account with it.
//
// Written out, not read from the code: every account preference the desk
// saves and applies.
const PREFERENCES = [
  'agentMemoryEnabled',
  'agentModelTier',
  'chatModelTier',
  'contrastMode',
  'lastAgentTier',
  'lastResearchProfile',
  'locale',
  'theme',
  'themePreset',
  'userBubbleTone',
]

function parse(relative: string): ts.SourceFile {
  const file = path.resolve(__dirname, relative)
  return ts.createSourceFile(file, readFileSync(file, 'utf-8'), ts.ScriptTarget.Latest, true)
}

function nodes<T extends ts.Node>(root: ts.Node, kind: (node: ts.Node) => node is T): T[] {
  const found: T[] = []
  function visit(node: ts.Node) {
    if (kind(node)) found.push(node)
    ts.forEachChild(node, visit)
  }
  visit(root)
  return found
}

const desk = parse('./ResearchDesk.tsx')
const projectTypes = parse('../project/types.ts')

function only<T>(items: T[], what: string): T {
  if (items.length !== 1) throw new Error(`Expected exactly one ${what}, found ${items.length}`)
  return items[0]
}

describe('the shell carries every account preference both ways', () => {
  it('declares exactly these preferences', () => {
    const declared = only(
      nodes(projectTypes, ts.isTypeAliasDeclaration)
        .filter((node) => node.name.text === 'ProjectPreferences'),
      'ProjectPreferences type',
    )
    if (!ts.isTypeLiteralNode(declared.type)) throw new Error('ProjectPreferences is no type literal')
    expect(declared.type.members.map((member) => member.name?.getText(projectTypes)).sort())
      .toEqual([...PREFERENCES].sort())
  })

  it('saves a change of each preference: the snapshot depends on all of them', () => {
    const declaration = only(
      nodes(desk, ts.isVariableDeclaration)
        .filter((node) => node.name.getText(desk) === 'currentPreferences'),
      'currentPreferences declaration',
    )
    const call = declaration.initializer
    if (!call || !ts.isCallExpression(call)) throw new Error('currentPreferences is not a call')
    const [factory, dependencies] = call.arguments
    if (!factory || !ts.isArrowFunction(factory)) throw new Error('No memo factory')
    let body: ts.Node = factory.body
    while (ts.isParenthesizedExpression(body)) body = body.expression
    if (!ts.isObjectLiteralExpression(body)) throw new Error('The memo builds no object literal')
    if (!dependencies || !ts.isArrayLiteralExpression(dependencies)) throw new Error('No memo dependencies')

    const fields = body.properties.map((property) => property.name?.getText(desk))
    const read = body.properties.map((property) => (
      ts.isShorthandPropertyAssignment(property)
        ? property.name.text
        : ts.isPropertyAssignment(property) ? property.initializer.getText(desk) : ''
    ))
    const listed = new Set(dependencies.elements.map((element) => element.getText(desk)))

    expect([...fields].sort()).toEqual([...PREFERENCES].sort())
    expect(read.filter((value) => !listed.has(value))).toEqual([])
  })

  it('applies each preference the account row or a project file brings', () => {
    const apply = only(
      nodes(desk, ts.isFunctionDeclaration)
        .filter((node) => node.name?.text === 'applyProjectPreferences'),
      'applyProjectPreferences',
    )
    const parameter = apply.parameters[0]?.name.getText(desk)
    if (!apply.body || !parameter) throw new Error('applyProjectPreferences has no body or parameter')
    const applied = nodes(apply.body, ts.isPropertyAccessExpression)
      .filter((node) => node.expression.getText(desk) === parameter)
      .map((node) => node.name.text)

    expect([...new Set(applied)].sort()).toEqual([...PREFERENCES].sort())
  })
})
