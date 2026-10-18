import { readFileSync } from 'node:fs'
import path from 'node:path'

import ts from 'typescript'
import { describe, expect, it } from 'vitest'

describe('where the workspace reads the requirement limits', () => {
  // Source pin: the workspace is not rendered in this lane. The demo has
  // no capability manifest, so without its mirror the rule cap and the
  // composed ceiling were never pre-checked there and every surface said
  // the server checks on sending. The mirror's numbers are pinned in
  // demo.test.ts; this pins that the workspace reads all three limits
  // from ONE block, and that the demo falls back to the mirror.
  const tree = ts.createSourceFile(
    'AgentWorkspace.tsx',
    readFileSync(path.resolve(__dirname, './AgentWorkspace.tsx'), 'utf-8'),
    ts.ScriptTarget.Latest,
    true,
  )
  function initializerOf(name: string): string | null {
    let found: string | null = null
    function visit(node: ts.Node) {
      if (ts.isVariableDeclaration(node) && node.name.getText(tree) === name && node.initializer) {
        found = node.initializer.getText(tree).replace(/\s+/g, ' ')
      }
      ts.forEachChild(node, visit)
    }
    visit(tree)
    return found
  }

  it('falls back to the demo mirror of the requirement block', () => {
    expect(initializerOf('publishedRequirement')).toBe(
      'agentBlock?.report_requirement ?? (demo ? DEMO_AGENT_REPORT_REQUIREMENT : null)',
    )
  })

  it('reads every limit from that block', () => {
    expect([
      initializerOf('reportGuidanceLimit'),
      initializerOf('reportRuleLimit'),
      initializerOf('reportComposedLimit'),
    ]).toEqual([
      'reportGuidanceMaxChars(publishedRequirement)',
      'reportRuleIdsMax(publishedRequirement)',
      'reportComposedCharsMax(publishedRequirement)',
    ])
  })
})
