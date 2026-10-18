import { readFileSync } from 'node:fs'
import path from 'node:path'

import ts from 'typescript'
import { describe, expect, it } from 'vitest'

// Source pin for wiring no node-environment test can mount (this repo's
// vitest lane is DOM-less by design; the composer renders in the browser).
// The pure halves are tested in contextCategories.test.ts and
// submitSettlement.test.ts (send first, settle after the answer); these
// pins hold the one hop between them and the request: the attached rules
// must reach BOTH submissions (the server and the demo twin) and leave the
// draft only through the settlement. The rules once were sent by nothing.
const tree = ts.createSourceFile(
  'AgentWorkspace.tsx',
  readFileSync(path.resolve(__dirname, './AgentWorkspace.tsx'), 'utf-8'),
  ts.ScriptTarget.Latest,
  true,
)

/** The object literal of the one call whose callee reads `callee`. */
function callArgument(callee: string): ts.ObjectLiteralExpression {
  const found: ts.ObjectLiteralExpression[] = []
  function visit(node: ts.Node) {
    if (
      ts.isCallExpression(node)
      && node.expression.getText(tree) === callee
      && node.arguments[0]
      && ts.isObjectLiteralExpression(node.arguments[0])
    ) {
      found.push(node.arguments[0])
    }
    ts.forEachChild(node, visit)
  }
  visit(tree)
  expect(found).toHaveLength(1)
  return found[0]
}

function property(literal: ts.ObjectLiteralExpression, name: string): string | null {
  for (const entry of literal.properties) {
    if (ts.isPropertyAssignment(entry) && entry.name.getText(tree) === name) {
      return entry.initializer.getText(tree)
    }
    if (ts.isShorthandPropertyAssignment(entry) && entry.name.getText(tree) === name) {
      return name
    }
  }
  return null
}

/** Every call of `callee` in the file, as source text. */
function calls(callee: string): string[] {
  const found: string[] = []
  function visit(node: ts.Node) {
    if (ts.isCallExpression(node) && node.expression.getText(tree) === callee) {
      found.push(node.getText(tree).replace(/\s+/g, ' '))
    }
    ts.forEachChild(node, visit)
  }
  visit(tree)
  return found
}

describe('the attached rules in the Agent Desk submission', () => {
  it('sends each attached rule once, to the server and to the demo', () => {
    expect(calls('ruleIdsForSubmit')).toContain('ruleIdsForSubmit(selectedRuleIds)')
    expect(property(callArgument('submitRun'), 'reportRuleIds'))
      .toBe('ruleIds.length > 0 ? ruleIds : undefined')
    expect(property(callArgument('demo.submit'), 'ruleIds')).toBe('ruleIds')
  })

  it('settles the server submission only through sendThenSettle', () => {
    // Clearing moved before the answer would lose the rules of a refused
    // send; `sendThenSettle` is tested for exactly that order.
    const settled = calls('sendThenSettle')
    expect(settled).toHaveLength(1)
    expect(settled[0]).toMatch(/^sendThenSettle\( \(\) => submitRun\(\{/)
    expect(settled[0]).toMatch(/\}\), settleSubmittedAttachments, \)$/)
    expect(calls('settleSubmittedAttachments')).toEqual(['settleSubmittedAttachments(true)'])
    expect(calls('settleRuleIdsAfterSubmit')).toEqual(['settleRuleIdsAfterSubmit(current, accepted)'])
  })

  it('lets the rules leave the draft nowhere but the settlement', () => {
    // Every place the setter is named: its declaration, the composer's
    // pills, and the settlement — an extra clear anywhere fails here.
    const places: string[] = []
    function place(node: ts.Node): string {
      for (let current: ts.Node | undefined = node.parent; current; current = current.parent) {
        if (ts.isArrayBindingPattern(current)) return 'declaration'
        if (ts.isJsxAttribute(current)) return `prop:${current.name.getText(tree)}`
        if (ts.isVariableDeclaration(current) && current.name.getText(tree) === 'settleSubmittedAttachments') {
          return 'settlement'
        }
      }
      return `elsewhere:${node.getStart(tree)}`
    }
    function visit(node: ts.Node) {
      // A dependency list names the setter without calling it.
      if (
        ts.isIdentifier(node) && node.text === 'setSelectedRuleIds'
        && !ts.isArrayLiteralExpression(node.parent)
      ) places.push(place(node))
      ts.forEachChild(node, visit)
    }
    visit(tree)
    expect(places.sort()).toEqual(['declaration', 'prop:onSelectedRuleIdsChange', 'settlement'])
  })

  it('offers the composer the rules keyed by the id the server resolves', () => {
    expect(calls('agentRuleSources')).toEqual([
      "agentRuleSources(chatRuleOptions(state, 'agent'), { localIds: Boolean(demo) })",
    ])
  })
})
