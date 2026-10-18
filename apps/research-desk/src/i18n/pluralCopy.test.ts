import { readFileSync, readdirSync } from 'node:fs'
import path from 'node:path'

import ts from 'typescript'
import { describe, expect, it } from 'vitest'

import { copyLeaves, type CopyLeaf, type CopyLocale } from './shippedCopy'

const srcRoot = path.resolve(import.meta.dirname, '..')
const read = (file: string) => readFileSync(path.join(srcRoot, file), 'utf8')

type Stream = { file: string; locale: CopyLocale; leaves: CopyLeaf[] }

/** The copy the plural guards read: both locales of the dictionary and the
 * two demos, whose German stands in the same transcript as live copy. */
function streams(): Stream[] {
  return [
    ...(['de', 'en'] as const).map((locale) => ({
      file: 'i18n/translations.ts', locale, leaves: copyLeaves(read('i18n/translations.ts'), 'i18n/translations.ts', locale),
    })),
    ...['features/agent/demo.ts', 'features/knowledge/demo.ts'].map((file) => ({
      file, locale: 'de' as const, leaves: copyLeaves(read(file), file),
    })),
  ]
}

const PARENTHETICAL_PLURAL = /\w\((e|n|en|s|er|es)\)/

/** Plural twins whose singular is not named `<key>One`. */
const IRREGULAR_TWINS: Record<string, string> = {
  'agent.task.instantMany': 'agent.task.instantOne',
  'agent.task.knowledgeMany': 'agent.task.knowledgeOne',
  'agent.timeline.childTasksFailed': 'agent.timeline.childTaskFailedOne',
  'agent.timeline.childSearchesDone': 'agent.timeline.childSearchDoneOne',
  'agent.timeline.childCheckRounds': 'agent.timeline.childCheckRoundOne',
  'agent.timeline.recapWebSearches': 'agent.timeline.recapWebSearchOne',
  'agent.timeline.recapKnowledgeSearches': 'agent.timeline.recapKnowledgeSearchOne',
  'agent.timeline.recapResearches': 'agent.timeline.recapResearchOne',
  'agent.timeline.recapSearches': 'agent.timeline.recapSearchOne',
  'sharingManagement.peopleCount': 'sharingManagement.onePerson',
  'knowledge.findWarningDuplicates': 'knowledge.findWarningDuplicate',
}

/** `{count}` templates without a singular, each with the reason a count of
 * one cannot reach it or no noun follows the number. */
const COUNT_NEUTRAL: Record<string, string> = {
  'agent.activity.parallelTasks': 'parallel work starts at two tasks; both callers render it only above one',
  'agent.activityOperations.operations': 'activityProgress renders it only above one operation',
  'agent.task.queryProgress': 'the query row renders it only above one request',
  'agent.composer.slashCapReached': 'no noun follows the number (max. {count})',
  'agent.timeline.evidenceReadProgress': 'no noun follows the number ({done}/{count} abgeschlossen)',
  'agent.timeline.gateActionsTitle': 'one action gets its own headline; the count title covers zero and several',
  'agent.canvas.commentLimit': 'filled with AGENT_CANVAS_COMMENT_LIMIT, a code constant of 20',
  'agent.canvas.child.attempt': 'an ordinal after its noun (Versuch 2), not a counted noun',
  'sharingManagement.pendingCount': 'no noun follows the number',
  'fileLibrary.referencedTooltip': 'a multiplier ({count}×), no noun follows the number',
  'fileLibrary.selectedCount': 'no noun follows the number',
  'fileLibrary.selectAllVisible': 'the number stands in parentheses after the verb, no noun follows',
  'vectorIndex.reindexPending': 'no noun follows the number',
  'vectorIndex.selectedCount': 'no noun follows the number',
  'vectorIndex.addCount': 'no noun follows the number',
  'vectorIndex.pausedDocuments': 'appended only when more than one document paused',
  'vectorIndex.resumeAllIndexing': 'shown only when more than one document paused; the number stands in parentheses, no noun follows',
  'vectorIndex.contextOmitted': 'no noun follows the number; it stands beside "indexed" and the tooltip names the chunks',
  'promptLibrary.contextResultsLimited': 'shown only at the cap, filled with contextPickerLimit, a code constant of 8',
  'chat.knowledgeScopeActive': 'no noun follows the number',
  'chat.chainIntermediate': 'a heading with its tally in parentheses; no noun follows the number',
}

/** Dictionary keys without any reader. They are listed for the operator's
 * removal decision instead of receiving a singular nobody would read. */
const UNREAD_AWAITING_DECISION = new Set([
  'knowledge.runFactCollection',
  'knowledge.runFactCollections',
  'knowledge.runFactHits',
])

/** Other counted placeholders followed by a noun (`key:placeholder`), each
 * with the reason the noun needs no singular. */
const PLACEHOLDER_NEUTRAL: Record<string, string> = {
  'agent.composer.reportCap:max': 'filled with the server constant MAX_ATTACHED_REPORTS = 3',
  'agent.plan.reportRulesCap:max': 'filled with the server constant REPORT_RULE_IDS_MAX = 3',
  'agent.timeline.recapElapsed:duration': 'a duration, not a count',
  'agent.tray.planSummary:web': 'a tool-kind tally: the word names the kind, like "intern"',
  'agent.tray.planSummary:synthesis': 'a tool-kind tally: the word names the kind, like "intern"',
  'knowledge.retrievalDegradedCapDetail:cap': 'German dative "Kandidaten" is the same for one; English names no noun',
  'knowledge.evidenceUsedDocsMeta:chunks': 'rendered only when the passages span more than one document',
  'knowledge.evidenceUsedDocsMeta:docs': 'rendered only when the passages span more than one document',
  'promptLibrary.categoryFunctionUsage:mention': 'a mention token, not a count',
  'promptLibrary.contextPlaceholderHint:placeholder': 'a placeholder token, not a count',
  'promptLibrary.categoryContextUsage:placeholder': 'a placeholder token, not a count',
  'promptLibrary.contextPromptPlaceholder:placeholder': 'a placeholder token, not a count',
  'adminSystem.unknownFeatureDescription:feature': 'a feature name, not a count',
}

/** A number followed by a noun, per language: German capitalises the noun
 * (lower-case attributes may stand between), English ends a plural noun in
 * -s or puts a plural verb right after the number ("{failed} need
 * attention" reads wrong for one). Function leaves render `${x}` as `{x}`
 * and join the stream; an UPPER_CASE interpolation is a module constant,
 * never a count. */
const COUNTED_NOUN: Record<CopyLocale, RegExp> = {
  de: /\{([a-z][\w.]*)\}[ \u00a0](?:[a-zäöüß-]+[ \u00a0]){0,3}[A-ZÄÖÜ]/g,
  en: /\{([a-z][\w.]*)\}[ \u00a0](?:(?:need|are|were|have|remain)\b|(?:[a-z-]+[ \u00a0]){0,3}(?!(?:is|was|has|as|this|its)\b)[a-z-]+s\b)/g,
}

function label(stream: Stream, leaf: CopyLeaf): string {
  return `${stream.file} [${stream.locale}] ${leaf.key}: ${leaf.text}`
}

/** Source files outside the plural helper that may still choose a
 * grammatical number by hand: the named Editor and Collaboration debt,
 * each until its own cut moves it to countLabel. */
const HAND_PLURAL_DEBT = new Set([
  'features/editor/inspector/EditorInspector.tsx',
  'features/editor/inspector/TeamCommentsPanel.tsx',
  'features/editor/inspector/collaborationAdapter.ts',
])

/** Singular keys read outside countLabel because the row IS one item. */
const SINGLE_ITEM_LABELS: Record<string, string> = {
  'features/agent/activityPresentation.ts:evidenceReadGroupFailedOne':
    'the title of one failed read row; the row is the single item it names',
}

function sourceFiles(): string[] {
  return (readdirSync(srcRoot, { recursive: true }) as string[])
    .filter((file) => /\.tsx?$/.test(file) && !/\.test\.tsx?$/.test(file))
    .filter((file) => !file.split(path.sep).includes('browser-tests'))
    .map((file) => file.split(path.sep).join('/'))
    .filter((file) => file !== 'i18n/plural.ts' && file !== 'i18n/shippedCopy.ts')
    .sort()
}

function unwrap(node: ts.Expression): ts.Expression {
  return ts.isParenthesizedExpression(node) ? unwrap(node.expression) : node
}

function comparesWithOne(node: ts.Expression): boolean {
  const condition = unwrap(node)
  if (!ts.isBinaryExpression(condition)) return false
  const operator = condition.operatorToken.kind
  if (operator !== ts.SyntaxKind.EqualsEqualsEqualsToken && operator !== ts.SyntaxKind.ExclamationEqualsEqualsToken) {
    return false
  }
  return [condition.left, condition.right].some((side) => ts.isNumericLiteral(side) && side.text === '1')
}

/** Methods whose result is still the text they were called on. */
const STRING_METHODS = new Set([
  'replace', 'replaceAll', 'toLocaleLowerCase', 'toLowerCase', 'toLocaleUpperCase', 'toUpperCase', 'trim',
])

function isText(node: ts.Expression): boolean {
  const value = unwrap(node)
  if (ts.isStringLiteralLike(value) || ts.isTemplateExpression(value) || ts.isPropertyAccessExpression(value)) return true
  return ts.isCallExpression(value)
    && ts.isPropertyAccessExpression(value.expression)
    && STRING_METHODS.has(value.expression.name.text)
}

function insideCountLabel(node: ts.Node): boolean {
  const call = node.parent
  return ts.isCallExpression(call)
    && ts.isIdentifier(call.expression)
    && call.expression.text === 'countLabel'
    && call.arguments.some((argument) => argument === node)
}

/** Every place a file picks a grammatical number itself: a `=== 1` choice
 * between two texts, or a `…One` singular read outside `countLabel`. */
function handPluralSites(file: string, source: string): string[] {
  const tree = ts.createSourceFile(file, source, ts.ScriptTarget.Latest, true)
  const sites: string[] = []
  const line = (node: ts.Node) => tree.getLineAndCharacterOfPosition(node.getStart(tree)).line + 1
  function visit(node: ts.Node) {
    if (ts.isConditionalExpression(node) && comparesWithOne(node.condition)
      && isText(node.whenTrue) && isText(node.whenFalse)) {
      sites.push(`${file}:${line(node)} ${node.getText(tree).replace(/\s+/g, ' ').slice(0, 90)}`)
    }
    if (ts.isPropertyAccessExpression(node) && /[a-z]One$/.test(node.name.text) && !insideCountLabel(node)
      && !(`${file}:${node.name.text}` in SINGLE_ITEM_LABELS)) {
      sites.push(`${file}:${line(node)} ${node.getText(tree)}`)
    }
    ts.forEachChild(node, visit)
  }
  visit(tree)
  return sites
}

describe('plural forms in shipped copy', () => {
  const all = streams()

  it('writes no parenthetical plural', () => {
    // "Analyse(n)", "Kommentar(e)", "source(s)" read as a form to fill in,
    // not as language; a counted text is a singular/plural pair instead.
    const found = all.flatMap((stream) => stream.leaves
      .filter((leaf) => PARENTHETICAL_PLURAL.test(leaf.text))
      .map((leaf) => label(stream, leaf)))
    expect(found).toEqual([])
  })

  it('gives every {count} template a singular twin or a stated neutrality', () => {
    const missing = all.flatMap((stream) => {
      const keys = new Set(stream.leaves.map((leaf) => leaf.key))
      return stream.leaves
        .filter((leaf) => leaf.text.includes('{count}') && !leaf.key.endsWith('One') && !leaf.countArgument)
        .filter((leaf) => !keys.has(`${leaf.key}One`))
        .filter((leaf) => !keys.has(IRREGULAR_TWINS[leaf.key] ?? '\0'))
        .filter((leaf) => !(leaf.key in COUNT_NEUTRAL) && !UNREAD_AWAITING_DECISION.has(leaf.key))
        .map((leaf) => label(stream, leaf))
    })
    expect(missing).toEqual([])
  })

  it('keeps every declared twin and neutrality current', () => {
    // A stale entry would excuse the next template that reuses its key.
    const dictionary = all.filter((stream) => stream.file === 'i18n/translations.ts')
    const stale = dictionary.flatMap((stream) => {
      const texts = new Map(stream.leaves.map((leaf) => [leaf.key, leaf.text]))
      return [
        ...Object.entries(IRREGULAR_TWINS)
          .filter(([key, twin]) => !texts.get(key)?.includes('{count}') || !texts.has(twin))
          .map(([key]) => `${stream.locale} twin ${key}`),
        ...[...Object.keys(COUNT_NEUTRAL), ...UNREAD_AWAITING_DECISION]
          .filter((key) => !texts.get(key)?.includes('{count}'))
          .map((key) => `${stream.locale} neutral ${key}`),
        ...Object.keys(PLACEHOLDER_NEUTRAL)
          .filter((entry) => {
            const [key, name] = entry.split(':')
            return !texts.get(key)?.includes(`{${name}}`)
          })
          .map((entry) => `${stream.locale} placeholder ${entry}`),
      ]
    })
    expect(stale).toEqual([])
  })

  it('names the neutrality of every other counted noun', () => {
    const found = all.flatMap((stream) => stream.leaves.flatMap((leaf) => [
      ...leaf.text.matchAll(COUNTED_NOUN[stream.locale]),
    ]
      .filter((match) => match[1] !== 'count')
      .filter((match) => !(`${leaf.key}:${match[1]}` in PLACEHOLDER_NEUTRAL))
      .map((match) => `${label(stream, leaf)} → ${match[0]}`)))
    expect(found).toEqual([])
  })

  it('chooses a grammatical number only through countLabel', () => {
    const found = sourceFiles()
      .filter((file) => !HAND_PLURAL_DEBT.has(file))
      .flatMap((file) => handPluralSites(file, read(file)))
    expect(found).toEqual([])
  })

  it('keeps the Editor and Collaboration debt list current', () => {
    // A file that no longer picks a number by hand leaves the list, so the
    // exemption never outlives the debt it names.
    const settled = [...HAND_PLURAL_DEBT].filter((file) => handPluralSites(file, read(file)).length === 0)
    expect(settled).toEqual([])
  })
})
