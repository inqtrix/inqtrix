import { readFileSync } from 'node:fs'
import path from 'node:path'

import ts from 'typescript'
import { describe, expect, it } from 'vitest'

import { sweepRetiredPlanDrafts } from './retiredPlanDrafts'

/** A browser storage area in memory, in the shape the sweep reads. */
function memoryStorage(entries: Record<string, string>) {
  const values = new Map(Object.entries(entries))
  return {
    get length() {
      return values.size
    },
    key: (index: number) => [...values.keys()][index] ?? null,
    removeItem: (key: string) => {
      values.delete(key)
    },
    keys: () => [...values.keys()].sort(),
  }
}

describe('the sweep of the retired plan drafts', () => {
  it('removes every plan draft and nothing else', () => {
    const storage = memoryStorage({
      'inqtrix.agent.plan-draft:run-1:v1': '{}',
      'inqtrix.agent.plan-draft:run-1:v2': '{}',
      'inqtrix.agent.plan-draft:run-2:v1': '{}',
      'inqtrix.editor.comment-draft:doc-1': '{}',
      'inqtrix.agent.plan-drafts': '{}',
      'prefix.inqtrix.agent.plan-draft:run-3:v1': '{}',
    })
    sweepRetiredPlanDrafts(storage)
    expect(storage.keys()).toEqual([
      'inqtrix.agent.plan-drafts',
      'inqtrix.editor.comment-draft:doc-1',
      'prefix.inqtrix.agent.plan-draft:run-3:v1',
    ])
  })

  it('leaves a storage that refuses to be read as it is', () => {
    const refusing = {
      get length(): number {
        throw new DOMException('blocked', 'SecurityError')
      },
      key: () => null,
      removeItem: () => undefined,
    }
    expect(() => sweepRetiredPlanDrafts(refusing)).not.toThrow()
    expect(() => sweepRetiredPlanDrafts(undefined)).not.toThrow()
  })

  it('runs once when the app starts, before anything renders and whoever signs in', () => {
    // Pinned at the entry point: the sweep must not depend on a sign-in
    // mode, an identity change or a surface being opened.
    const source = ts.createSourceFile(
      'main.tsx',
      readFileSync(path.resolve(import.meta.dirname, '../../main.tsx'), 'utf8'),
      ts.ScriptTarget.Latest,
      true,
    )
    const topLevelCalls = source.statements.flatMap((statement, index) => (
      ts.isExpressionStatement(statement) && ts.isCallExpression(statement.expression)
        ? [{ callee: statement.expression.expression.getText(source), index }]
        : []
    ))
    const sweep = topLevelCalls.filter((call) => call.callee === 'sweepRetiredPlanDrafts')
    expect(sweep).toHaveLength(1)
    const render = topLevelCalls.find((call) => call.callee.startsWith('createRoot('))
    expect(render).toBeDefined()
    expect(sweep[0].index).toBeLessThan(render!.index)
  })
})
