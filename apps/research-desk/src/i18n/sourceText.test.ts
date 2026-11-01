import { readdirSync, readFileSync } from 'node:fs'
import { join, relative } from 'node:path'
import { fileURLToPath } from 'node:url'

import ts from 'typescript'
import { describe, expect, it } from 'vitest'

import { translations } from './translations'

const sourceRoot = fileURLToPath(new URL('..', import.meta.url))

function productFiles(directory: string): string[] {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const path = join(directory, entry.name)
    if (entry.isDirectory()) return entry.name === 'browser-tests' ? [] : productFiles(path)
    return /\.tsx?$/.test(entry.name) && !/\.test\.tsx?$/.test(entry.name) ? [path] : []
  })
}

const sources = productFiles(sourceRoot).map((path) => ts.createSourceFile(
  path,
  readFileSync(path, 'utf8'),
  ts.ScriptTarget.Latest,
  true,
  path.endsWith('.tsx') ? ts.ScriptKind.TSX : ts.ScriptKind.TS,
))

/** Every node matching the check, as `file:line source`, so a red run names the place. */
function offenders(check: (node: ts.Node, source: ts.SourceFile) => boolean): string[] {
  const found: string[] = []
  for (const source of sources) {
    const visit = (node: ts.Node): void => {
      if (check(node, source)) {
        const { line } = source.getLineAndCharacterOfPosition(node.getStart(source))
        const text = node.getText(source).replace(/\s+/g, ' ').slice(0, 100)
        found.push(`${relative(sourceRoot, source.fileName)}:${line + 1} ${text}`)
      }
      ts.forEachChild(node, visit)
    }
    visit(source)
  }
  return found
}

describe('text in the product source', () => {
  it('sees the whole product, not a part of it', () => {
    // A scan that reads nothing is green for the wrong reason.
    expect(sources.length).toBeGreaterThan(400)
    expect(sources.map((source) => relative(sourceRoot, source.fileName)))
      .toContain('features/fileLibrary/FileItem.tsx')
  })

  it('fills text through a function, so a $ in the value stays a $', () => {
    // `replace(pattern, text)` reads $&, $$, $` and $' in the text: a file
    // named "Budget $& Plan" came out as "Budget {label} Plan". A function's
    // result is taken as written. Literals and String(number) carry no user text.
    const unsafe = offenders((node, source) => {
      if (!ts.isCallExpression(node) || node.arguments.length !== 2) return false
      if (!ts.isPropertyAccessExpression(node.expression)) return false
      if (!['replace', 'replaceAll'].includes(node.expression.name.text)) return false
      const value = node.arguments[1]
      return !ts.isStringLiteralLike(value)
        && !ts.isArrowFunction(value)
        && !ts.isFunctionExpression(value)
        && !(ts.isCallExpression(value) && value.expression.getText(source) === 'String')
    })
    expect(unsafe).toEqual([])
  })

  it('takes the names of controls from the translations', () => {
    const named = new Set(['aria-label', 'ariaLabel', 'closeLabel', 'label', 'title'])
    const literal = offenders((node, source) => (
      ts.isJsxAttribute(node)
      && named.has(node.name.getText(source))
      && node.initializer !== undefined
      && ts.isStringLiteral(node.initializer)
      && /[A-Za-z]/.test(node.initializer.text)
    ))
    expect(literal).toEqual([])
  })

  it('names the handle of the report panel in both languages', () => {
    // Bound stated: this reads the source, because the handle only renders in a
    // desktop layout this DOM-less lane cannot measure.
    const workspace = readFileSync(
      join(sourceRoot, 'features/researchDesk/components/ResearchWorkspace.tsx'),
      'utf8',
    )
    expect(workspace).toContain('aria-label={t.report.resizePanel}')
    expect(translations.de.report.resizePanel).toBe('Breite des Reports anpassen')
    expect(translations.en.report.resizePanel).toBe('Resize report panel')
  })

  it('has every dialog name its close button through the translations', () => {
    const unnamed = offenders((node, source) => {
      if (!ts.isJsxOpeningElement(node) && !ts.isJsxSelfClosingElement(node)) return false
      if (node.tagName.getText(source) !== 'Dialog') return false
      return !node.attributes.properties.some((property) => (
        ts.isJsxAttribute(property)
        && property.name.getText(source) === 'closeLabel'
        && property.initializer !== undefined
        && ts.isJsxExpression(property.initializer)
      ))
    })
    expect(unnamed).toEqual([])
  })
})
