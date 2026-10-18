import { readFileSync } from 'node:fs'
import path from 'node:path'

import ts from 'typescript'
import { describe, expect, it } from 'vitest'

import { draftFieldSetter, emptyAgentComposerDraft, type AgentComposerDraft } from './composerDraft'

describe('one field of the agent draft', () => {
  function harness() {
    let draft = emptyAgentComposerDraft()
    const setDraft = (next: AgentComposerDraft | ((current: AgentComposerDraft) => AgentComposerDraft)) => {
      draft = typeof next === 'function' ? next(draft) : next
    }
    return { read: () => draft, setDraft }
  }

  it('takes a value or an updater of the field, like useState', () => {
    const { read, setDraft } = harness()
    draftFieldSetter(setDraft, 'reportIds')(['r1'])
    draftFieldSetter(setDraft, 'reportIds')((current) => [...current, 'r2'])
    expect(read().reportIds).toEqual(['r1', 'r2'])
    expect(read().ruleIds).toEqual([])
  })

  it('keeps the draft itself when the field does not change', () => {
    const { read, setDraft } = harness()
    const before = read()
    draftFieldSetter(setDraft, 'responseForm')('auto')
    expect(read()).toBe(before)
  })
})

describe('the Agent Desk holds the whole draft outside the workspace', () => {
  // The desk unmounts the workspace on every view switch: a draft field
  // kept as the workspace's own state comes back empty. Every field of the
  // draft is therefore read through `useDraftField` — a field missing here
  // is one a view switch would lose (the canvas pin and the comment queue
  // included, which the demo cannot exercise in the browser).
  const source = (file: string) => ts.createSourceFile(
    file,
    readFileSync(path.resolve(__dirname, file), 'utf-8'),
    ts.ScriptTarget.Latest,
    true,
  )

  it('reads every field of the draft type through useDraftField', () => {
    const draftFile = source('./composerDraft.ts')
    const fields: string[] = []
    ts.forEachChild(draftFile, (node) => {
      if (ts.isTypeAliasDeclaration(node) && node.name.text === 'AgentComposerDraft'
        && ts.isTypeLiteralNode(node.type)) {
        for (const member of node.type.members) {
          if (member.name && ts.isIdentifier(member.name)) fields.push(member.name.text)
        }
      }
    })
    const read: string[] = []
    const workspace = source('./AgentWorkspace.tsx')
    function visit(node: ts.Node) {
      if (ts.isCallExpression(node) && node.expression.getText(workspace) === 'useDraftField') {
        const key = node.arguments[2]
        if (key && ts.isStringLiteral(key)) read.push(key.text)
      }
      ts.forEachChild(node, visit)
    }
    visit(workspace)
    expect(fields.length).toBeGreaterThan(8)
    expect([...read].sort()).toEqual([...fields].sort())
  })
})
