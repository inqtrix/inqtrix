import ts from 'typescript'

/**
 * Test support for the copy guards (orthography, plural forms): the shipped
 * copy of one source file, read from literal syntax. Importing a locale
 * object alone would miss copy inside its interpolation functions, so the
 * guards parse the source instead. Only tests import this module — it pulls
 * in the TypeScript compiler, which the bundle must never carry.
 */
export type CopyLocale = 'de' | 'en'

export type CopyLeaf = {
  /** Dotted property path inside the locale, or inside the file for
   * sources without locales (demos, seeds). */
  key: string
  /** The leaf as one template: an interpolation reads `{name}` for an
   * identifier or member access and `{…}` for any other expression, whose
   * own literals are leaves of their own. */
  text: string
  /** The literal fragments alone — the prose without interpolation syntax. */
  fragments: string[]
  /** Whether the leaf is returned by a function-valued entry. */
  inFunction: boolean
  /** Whether the leaf is the singular or plural phrase handed to
   * `countLabel` in code (the pair is declared at the call). */
  countArgument: boolean
}

// These fields are identifiers, not prose. Seed asset labels and skill
// parameter names stay compatible with saved references and placeholders.
const IDENTIFIER_KEYS = new Set([
  'id', 'collectionId', 'groupId', 'runId', 'profile', 'profileId',
  'rag_default_profile', 'web_child_profile', 'web_child_ceiling',
])

function isLocaleFile(file: string): boolean {
  return file.endsWith('translations.ts') || file.endsWith('editorCopy.ts')
}

function isCountArgument(node: ts.Node): boolean {
  const call = node.parent
  return ts.isCallExpression(call)
    && ts.isIdentifier(call.expression)
    && call.expression.text === 'countLabel'
    && (call.arguments[1] === node || call.arguments[2] === node)
}

function interpolation(expression: ts.Expression, tree: ts.SourceFile): string {
  return ts.isIdentifier(expression) || ts.isPropertyAccessExpression(expression)
    ? `{${expression.getText(tree)}}`
    : '{…}'
}

/** Every copy leaf of `file` in `locale`. A locale file (translations,
 * editor copy) contributes only the chosen locale's subtree; any other
 * source contributes the whole file. */
export function copyLeaves(source: string, file: string, locale: CopyLocale = 'de'): CopyLeaf[] {
  const tree = ts.createSourceFile(file, source, ts.ScriptTarget.Latest, true)
  const roots: ts.Node[] = []
  if (isLocaleFile(file)) {
    function findLocale(node: ts.Node) {
      if (ts.isPropertyAssignment(node) && node.name.getText(tree) === locale) roots.push(node.initializer)
      else ts.forEachChild(node, findLocale)
    }
    findLocale(tree)
    if (roots.length !== 1) throw new Error(`Expected one ${locale} locale in ${file}, found ${roots.length}`)
  } else roots.push(tree)

  const leaves: CopyLeaf[] = []
  function visit(node: ts.Node, path: readonly string[], inFunction: boolean) {
    if (ts.isPropertyAssignment(node)) {
      const name = node.name.getText(tree)
      visit(node.initializer, [...path, name], inFunction)
      return
    }
    if (ts.isMethodDeclaration(node)) {
      const name = node.name.getText(tree)
      ts.forEachChild(node, (child) => {
        if (child !== node.name) visit(child, [...path, name], true)
      })
      return
    }
    if (ts.isArrowFunction(node) || ts.isFunctionExpression(node)) {
      ts.forEachChild(node, (child) => visit(child, path, true))
      return
    }
    const skipped = () => {
      const parent = node.parent
      const key = ts.isPropertyAssignment(parent) ? parent.name.getText(tree) : ''
      return ts.isLiteralTypeNode(parent)
        || ts.isImportDeclaration(parent)
        || IDENTIFIER_KEYS.has(key)
        || (file.endsWith('seedProject.ts') && key === 'label')
        || (file.endsWith('useSkillsApi.ts') && key === 'name')
    }
    if (ts.isStringLiteralLike(node)) {
      if (!skipped()) {
        leaves.push({
          key: path.join('.'), text: node.text, fragments: [node.text], inFunction, countArgument: isCountArgument(node),
        })
      }
      return
    }
    if (ts.isTemplateExpression(node)) {
      if (!skipped()) {
        const fragments = [node.head.text, ...node.templateSpans.map((span) => span.literal.text)]
        const text = node.head.text + node.templateSpans
          .map((span) => interpolation(span.expression, tree) + span.literal.text)
          .join('')
        leaves.push({ key: path.join('.'), text, fragments, inFunction, countArgument: isCountArgument(node) })
      }
      node.templateSpans.forEach((span) => visit(span.expression, path, inFunction))
      return
    }
    ts.forEachChild(node, (child) => visit(child, path, inFunction))
  }
  roots.forEach((root) => visit(root, [], false))
  if (leaves.length === 0) throw new Error(`No copy found in ${file}`)
  return leaves
}

/** The prose of `file` as one text, for word-level checks. */
export function sourceCopy(source: string, file: string, locale: CopyLocale = 'de'): string {
  return copyLeaves(source, file, locale).flatMap((leaf) => leaf.fragments).join('\n')
}
