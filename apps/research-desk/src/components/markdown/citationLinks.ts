import {
  canonicalizeCitationGroups,
  citationGroupSpans,
  type CitationLabelPredicate,
  opaqueSpans,
  prosePieces,
} from './citationGrammar'

export type { CitationLabelPredicate } from './citationGrammar'

const CITATION_HREF_PREFIX = '#kref-'
// A bracket whose `[` is backslash-escaped (an odd run of backslashes before
// it), up to its closing bracket, escaped or not.
const ESCAPED_BRACKET = /(?<!\\)(?:\\\\)*\\\[(?:[^[\]\\\n]|\\[^\n]){0,200}?\\?\]/g

type LinkifyOptions = {
  requireKnownBracketed?: boolean
  /**
   * Also redirect a label that ALREADY links to an external URL.
   *
   * Only for surfaces whose citations belong in an evidence panel: the
   * panel carries the excerpt, the provenance and the verification status,
   * and the URL stays reachable from there. Restricted to labels present in
   * ``knownLabels``, so an ordinary link in prose is never rewritten.
   */
  redirectKnownExternalLinks?: boolean
}

/**
 * Turn known citation labels into synthetic local Markdown links without
 * coupling the shared Markdown renderer to one evidence system.
 *
 * Grouped brackets are written singly first (the citation grammar), and
 * only prose is read: code stays code, and a platform marker or a bracket
 * no reader can parse (``[unbelegt: K2]``, ``[K3–K5]``) stays as written,
 * so no chip claims a source the text does not cite. A backslash-escaped
 * bracket (``\[K2, W7\]``) is literal text as a whole: no label inside it
 * becomes a chip, bare or bracketed.
 *
 * Bracketed labels are linkified when they belong to the caller's vocabulary.
 * Bare labels are intentionally stricter: they must also occur in
 * ``knownLabels`` so ordinary prose cannot become a dead evidence link.
 */
export function linkifyCitationLabels(
  markdown: string,
  isCitationLabel: CitationLabelPredicate,
  knownLabels?: ReadonlySet<string>,
  options: LinkifyOptions = {},
): string {
  return prosePieces(canonicalizeCitationGroups(markdown, isCitationLabel))
    .map((piece) => {
      if (!piece.prose) return piece.text
      let out = ''
      let cursor = 0
      for (const span of literalSpans(piece.text, isCitationLabel)) {
        if (span.start < cursor) continue
        out += linkifyProse(piece.text.slice(cursor, span.start), isCitationLabel, knownLabels, options)
        out += span.text
        cursor = span.end
      }
      return out + linkifyProse(piece.text.slice(cursor), isCitationLabel, knownLabels, options)
    })
    .join('')
}

// One label in a bracket that does not open a link.
const SINGLE_LABEL_BRACKET = /(?<!\\)\[([A-Z][0-9]+)\](?!\()/g
const BARE_LABEL = /(^|[^\w[\]()/#-])([A-Z][0-9]+)\b/g

/**
 * Where reading links a label in one prose *text*, for a surface that cannot
 * rewrite its text (an editor): a known label in a citation bracket, single
 * or grouped (`[K1]`, `[K2, W7]`, each member once), and a known label
 * standing alone. These are the decisions of `linkifyCitationLabels` with
 * `requireKnownBracketed`: a bracket no reader parses, a platform marker and
 * an escaped bracket stay text. A glued run of labels (`K6K15`) stays text
 * here.
 */
export function citationLabelSpans(
  text: string,
  isCitationLabel: CitationLabelPredicate,
  knownLabels: ReadonlySet<string>,
): { start: number; end: number; label: string }[] {
  // Every bracket of the text, citation or not: a bare label is one outside
  // all of them. A citation bracket never lies inside an escaped or opaque
  // one, since neither holds an unescaped bracket.
  const brackets: { start: number; end: number }[] = [...literalSpans(text, isCitationLabel)]
  const spans: { start: number; end: number; label: string }[] = []
  const add = (start: number, label: string) => {
    if (knownLabels.has(label)) spans.push({ start, end: start + label.length, label })
  }
  for (const group of citationGroupSpans(text, isCitationLabel)) {
    for (const member of group.labels) add(member.start, member.label)
  }
  for (const bracket of text.matchAll(SINGLE_LABEL_BRACKET)) {
    brackets.push({ start: bracket.index, end: bracket.index + bracket[0].length })
    if (isCitationLabel(bracket[1])) add(bracket.index + 1, bracket[1])
  }
  for (const match of text.matchAll(BARE_LABEL)) {
    const label = match[2]
    const start = match.index + match[1].length
    if (!isCitationLabel(label)) continue
    if (brackets.some((span) => span.start <= start && start < span.end)) continue
    add(start, label)
  }
  return spans.sort((left, right) => left.start - right.start)
}

/** The spans of one prose *text* that stay as written, sorted by position:
 * the grammar's opaque brackets and every escaped bracket. */
function literalSpans(
  text: string,
  isCitationLabel: CitationLabelPredicate,
): { start: number; end: number; text: string }[] {
  const escaped = [...text.matchAll(ESCAPED_BRACKET)].map((match) => ({
    start: match.index,
    end: match.index + match[0].length,
    text: match[0],
  }))
  return [...opaqueSpans(text, isCitationLabel), ...escaped]
    .sort((left, right) => left.start - right.start)
}

function linkifyProse(
  markdown: string,
  isCitationLabel: CitationLabelPredicate,
  knownLabels: ReadonlySet<string> | undefined,
  options: LinkifyOptions,
): string {
  let out = markdown.replace(
    /(!?)\[([A-Z]\d+)\](?!\()/g,
    (match, bang: string, label: string, offset: number) => (
      isCitationLabel(label)
      && (!options.requireKnownBracketed || knownLabels?.has(label))
        ? `${textBang(bang, markdown, offset)}${citationLink(label)}`
        : match
    ),
  )
  if (options.redirectKnownExternalLinks && knownLabels) {
    // [E3](https://…) -> [E3](#kref-E3). A label already pointing at #kref-
    // is left alone; so is any label the reference set does not know.
    out = out.replace(
      /\[([A-Z]\d+)\]\((?!#kref-)[^)\s]+\)/g,
      (match, label: string) => (
        isCitationLabel(label) && knownLabels.has(label)
          ? citationLink(label)
          : match
      ),
    )
  }
  if (!knownLabels || knownLabels.size === 0) return out

  out = out.replace(
    /(^|[^\w[\]()/#-])((?:[A-Z]\d+){2,})\b/g,
    (match, prefix: string, run: string, offset: number, text: string) => {
      const labels = splitKnownCitationRun(run, knownLabels, isCitationLabel)
      return labels
        ? `${textBang(prefix, text, offset)}${labels.map((label) => citationLink(label)).join('')}`
        : match
    },
  )
  return out.replace(
    /(^|[^\w[\]()/#-])([A-Z]\d+)\b/g,
    (match, prefix: string, label: string, offset: number, text: string) => (
      isCitationLabel(label) && knownLabels.has(label)
        ? `${textBang(prefix, text, offset)}${citationLink(label)}`
        : match
    ),
  )
}

/** *prefix*, the text at *offset* right before a new citation link, kept as
 * text: a `!` there would make the link an image (`![K1](#kref-K1)`), so an
 * unescaped one is written escaped. */
function textBang(prefix: string, text: string, offset: number): string {
  if (prefix !== '!') return prefix
  let backslashes = 0
  while (text[offset - 1 - backslashes] === '\\') backslashes += 1
  return backslashes % 2 === 0 ? '\\!' : '!'
}

/** Whether *href* is a synthetic evidence link of {@link linkifyCitationLabels}. */
export function isCitationHref(href: unknown): boolean {
  return typeof href === 'string' && href.includes(CITATION_HREF_PREFIX)
}

/** Extract a caller-approved label from a synthetic evidence link. */
export function citationLabelFromHref(
  href: string | null | undefined,
  isCitationLabel: CitationLabelPredicate,
): string | null {
  if (!href) return null
  const index = href.indexOf(CITATION_HREF_PREFIX)
  if (index === -1) return null
  const label = href.slice(index + CITATION_HREF_PREFIX.length)
  return isCitationLabel(label) ? label : null
}

function citationLink(label: string): string {
  return `[${label}](${CITATION_HREF_PREFIX}${label})`
}

function splitKnownCitationRun(
  run: string,
  knownLabels: ReadonlySet<string>,
  isCitationLabel: CitationLabelPredicate,
): string[] | null {
  const labels = [...knownLabels].filter(isCitationLabel)
  const memo = new Map<number, string[] | null>()
  const splitFrom = (offset: number): string[] | null => {
    if (offset === run.length) return []
    const cached = memo.get(offset)
    if (cached !== undefined) return cached
    const candidates = labels
      .filter((label) => run.startsWith(label, offset))
      .sort((a, b) => b.length - a.length)
    for (const label of candidates) {
      const rest = splitFrom(offset + label.length)
      if (rest) {
        const result = [label, ...rest]
        memo.set(offset, result)
        return result
      }
    }
    memo.set(offset, null)
    return null
  }
  const result = splitFrom(0)
  return result && result.length > 1 ? result : null
}
