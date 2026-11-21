import { existsSync, readFileSync, readdirSync } from 'node:fs'
import path from 'node:path'

import ts from 'typescript'
import { describe, expect, it } from 'vitest'

import { translations } from '@/i18n/translations'

// The desk runs on the kernel alone. The retired mission engine's surfaces
// (plan view and gate, task views, the mission card of a delegation) and
// the server routes only that engine answered must not come back through
// an import or a request.
const srcRoot = path.resolve(import.meta.dirname, '../..')

function sourceFiles(): string[] {
  return (readdirSync(srcRoot, { recursive: true }) as string[])
    .filter((file) => /\.tsx?$/.test(file))
    .map((file) => file.split(path.sep).join('/'))
    .sort()
}

function tree(file: string): ts.SourceFile {
  return ts.createSourceFile(
    file,
    readFileSync(path.join(srcRoot, file), 'utf8'),
    ts.ScriptTarget.Latest,
    true,
  )
}

/** Every module specifier a file names: imports, re-exports, dynamic imports
 * and module mocks. */
function specifiers(source: ts.SourceFile): string[] {
  const found: string[] = []
  function visit(node: ts.Node) {
    if (
      (ts.isImportDeclaration(node) || ts.isExportDeclaration(node))
      && node.moduleSpecifier
      && ts.isStringLiteral(node.moduleSpecifier)
    ) {
      found.push(node.moduleSpecifier.text)
    }
    if (ts.isCallExpression(node) && node.arguments[0] && ts.isStringLiteralLike(node.arguments[0])) {
      const callee = node.expression.getText(source)
      if (node.expression.kind === ts.SyntaxKind.ImportKeyword || callee === 'vi.mock') {
        found.push(node.arguments[0].text)
      }
    }
    ts.forEachChild(node, visit)
  }
  visit(source)
  return found
}

/** The `src`-relative path a specifier points at, or null for a package. */
function resolved(file: string, specifier: string): string | null {
  if (specifier.startsWith('@/')) return specifier.slice(2)
  if (!specifier.startsWith('.')) return null
  return path.posix.normalize(path.posix.join(path.posix.dirname(file), specifier))
}

const RETIRED_MODULES = [
  /^features\/agent\/plan(\/|$)/,
  /(^|\/)childMission(\.tsx?)?$/,
  /(^|\/)ChildMissionCard(\.tsx?)?$/,
  /(^|\/)taskResultPrefetch(\.tsx?)?$/,
  /(^|\/)usePatchReview(\.tsx?)?$/,
]

describe('a desk without the mission engine', () => {
  it('imports nothing of the retired mission surfaces', () => {
    const imports = sourceFiles().flatMap((file) => specifiers(tree(file))
      .map((specifier) => ({ file, target: resolved(file, specifier) }))
      .filter(({ target }) => target !== null && RETIRED_MODULES.some((pattern) => pattern.test(target)))
      .map(({ file, target }) => `${file} -> ${target}`))
    expect(imports).toEqual([])
  })

  it('keeps none of their files', () => {
    const left = [
      'features/agent/plan',
      'features/agent/childMission.ts',
      'features/agent/timeline/ChildMissionCard.tsx',
      'features/agent/taskResultPrefetch.ts',
      'features/agent/patch/usePatchReview.ts',
    ].filter((file) => existsSync(path.join(srcRoot, file)))
    expect(left).toEqual([])
  })

  it('asks the server for no plan and no task of a run', () => {
    // `/v1/runs/{id}/plan` and `/v1/runs/{id}/tasks/{task}/…` belong to the
    // retired engine; a request for them reads a stored mission's plan or
    // task rows, which no surface shows any more.
    const requests: string[] = []
    for (const file of sourceFiles().filter((name) => !/\.test\.tsx?$/.test(name))) {
      const source = tree(file)
      const visit = (node: ts.Node) => {
        let text: string | null = null
        if (ts.isStringLiteralLike(node)) text = node.text
        if (ts.isTemplateExpression(node)) {
          // Each interpolation reads as a placeholder segment of the path.
          text = [node.head.text, ...node.templateSpans.map((span) => `\${…}${span.literal.text}`)].join('')
        }
        if (text !== null && /\/v1\/runs\/.*\/(plan|tasks)(?![\w-])/.test(text)) {
          requests.push(`${file}: ${text}`)
        }
        ts.forEachChild(node, visit)
      }
      visit(source)
    }
    expect(requests).toEqual([])
  })

  it('names the retired engine in no text it shows, in either language', () => {
    // Stored runs keep their plan, task and decision words; none of them
    // and no text of the kernel desk says "mission" to the reader.
    const texts: string[] = []
    const collect = (value: unknown) => {
      if (typeof value === 'string') texts.push(value)
      else if (value && typeof value === 'object') Object.values(value).forEach(collect)
    }
    collect(translations)
    expect(texts.filter((text) => /\bmission/i.test(text))).toEqual([])
  })
})
