import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import {
  readFileSync,
  readdirSync,
} from 'node:fs'
import { basename, dirname, resolve } from 'node:path'
import { describe, test } from 'node:test'
import { fileURLToPath } from 'node:url'

import { readInqtrixVersion } from './report.ts'

const REPOSITORY_ROOT = resolve(
  dirname(fileURLToPath(import.meta.url)),
  '../..',
)

const PRIVATE_PATH_EXAMPLES = [
  'AGENTS.md',
  'agents.md',
  'Agents.md',
  'CLAUDE.md',
  'claude.md',
  'Claude.md',
  'CURSOR.md',
  'cursor.md',
  'Cursor.md',
  '.cursor/rules/private.mdc',
  '.claude/private.md',
] as const

const FORBIDDEN_SOURCE_MARKERS = [
  {
    label: 'private plan milestone',
    pattern: new RegExp('\\b' + 'plan[ -]' + 'M\\d+[a-z]?', 'i'),
  },
  {
    label: 'private program phase',
    pattern: new RegExp('\\b' + 'Programm-' + '\\d+', 'i'),
  },
  {
    label: 'private decision identifier',
    pattern: new RegExp('\\b' + '(?:decision|Entscheidung)\\s+' + 'E\\d+', 'i'),
  },
  {
    label: 'private ADR identifier',
    pattern: new RegExp('\\b' + 'ADR-' + '[A-Z][A-Z0-9_-]*-\\d+', 'i'),
  },
  {
    label: 'priority-labelled implementation history',
    pattern: new RegExp(
      '\\b' + 'P\\d+\\s*(?:fix|bug|finding|review|regression|lesson|incident)',
      'i',
    ),
  },
  {
    label: 'incident chronology',
    pattern: new RegExp('\\b' + 'live\\s+incident' + '\\b', 'i'),
  },
  {
    label: 'dated implementation decision',
    pattern: new RegExp(
      '\\b' + '(?:user\\s+decision|deferred)\\s+20\\d{2}' + '\\b',
      'i',
    ),
  },
  {
    label: 'migration creation chronology',
    pattern: new RegExp('\\b' + 'Create\\s+Date\\s*:', 'i'),
  },
  {
    label: 'removed baseline metadata',
    pattern: new RegExp(
      '(?:`|")' + '_established' + '(?:`|")',
      'i',
    ),
  },
] as const

// Names of the product that have the shape of a short tag: the labels of
// knowledge and web sources, and technical names of at most three letters and
// a number.
const PRODUCT_NAMES_OF_TAG_SHAPE = '(?:[KW]\\d{1,3}|S3|SHA-\\d{1,3}|UTF-8)'

// Identifiers a working process gives its own stages, runs, findings and
// decisions. They mean nothing to a reader of the versioned source, so a
// comment, docstring or test name states the behaviour instead. Each
// pattern names a form, never an actual identifier, and each is proven
// below against a planted line and against legitimate product text.
const WORK_STAGE_IDENTIFIERS = [
  {
    label: 'internal work-stage identifier: dotted stage',
    pattern: new RegExp('\\b' + 'N' + '\\d+\\.\\d+[a-z]?\\b'),
  },
  {
    label: 'internal work-stage identifier: lettered stage',
    pattern: new RegExp('(?<![\\w.-])' + '\\d[a-dA-D]' + '\\.\\d+[a-z]?(?![\\w.])'),
  },
  {
    label: 'internal work-stage identifier: lettered phase',
    pattern: new RegExp('\\b' + 'phase[ -]?' + '\\d+[a-z]\\b', 'i'),
  },
  {
    label: 'internal work-stage identifier: lettered milestone',
    pattern: new RegExp('(?<![\\w.-])' + 'M' + '\\d{1,2}[a-d]\\b'),
  },
  {
    label: 'internal work-stage identifier: run letter',
    pattern: new RegExp('\\b' + 'Lauf' + '\\s+[A-Z0-9]\\b'),
  },
  {
    label: 'internal work-stage identifier: numbered counter-case',
    pattern: new RegExp('\\b' + '(?:counter-?case|Gegenfall)' + '\\s*\\d+', 'i'),
  },
  {
    label: 'internal work-stage identifier: finding id',
    pattern: new RegExp(
      '\\b' + 'F-' + '[A-Z0-9]{1,8}-[A-Z0-9]{1,16}\\b'
      + '|\\b[A-Z]+\\d*-' + 'F-' + '\\d+\\b',
    ),
  },
  {
    label: 'internal work-stage identifier: numbered finding',
    pattern: new RegExp('(?:befund|finding)' + '\\s*#\\s*\\d+', 'i'),
  },
  {
    label: 'internal work-stage identifier: review id',
    pattern: new RegExp('\\b' + 'review' + '\\s+[A-Z]\\d+(?:-[A-Z]\\d+)?\\b'),
  },
  {
    label: 'internal work-stage identifier: decision id',
    pattern: new RegExp(
      '\\b' + 'E-' + '[A-Z]+\\d+-\\d+\\b'
      + '|\\b' + 'P\\d+-' + 'E\\d+\\b',
    ),
  },
  {
    label: 'internal work-stage identifier: numbered decision',
    pattern: new RegExp('\\b' + 'decision' + '\\s*#\\s*\\d+', 'i'),
  },
  {
    label: 'internal work-stage identifier: register id',
    pattern: new RegExp('\\b' + 'R-' + '\\d{2,3}\\b'),
  },
  {
    label: 'internal work-stage identifier: invariant tag',
    pattern: new RegExp('\\b' + 'invariant' + '\\s+I\\d+\\b', 'i'),
  },
  {
    label: 'internal work-stage identifier: plan package',
    pattern: new RegExp('\\(' + '(?:plan|Paket|package)' + '\\s+[A-Z0-9][\\w.]*\\)'),
  },
  {
    label: 'internal work-stage identifier: priority stage',
    pattern: new RegExp(
      '\\(' + 'P' + '\\d{1,2}(?:\\.\\d+)?[A-Za-z]?[,)]'
      + '|\\b' + 'P' + '\\d{1,2}(?:\\.\\d+)?[A-Z]\\b'
      + '|\\b' + 'P' + '\\d{1,2}[a-d]:',
    ),
  },
  {
    label: 'internal work-stage identifier: work-package id',
    pattern: new RegExp('\\b' + 'WP-' + '[A-Z]-[A-Z]\\b'),
  },
  {
    label: 'internal work-stage identifier: operator decision id',
    pattern: new RegExp('\\b' + 'O-' + '\\d{1,3}\\b'),
  },
  {
    label: 'internal work-stage identifier: plan phase id',
    pattern: new RegExp('\\b' + 'AU-' + '\\d{1,2}[a-z]?\\b'),
  },
  {
    // Tags a design or architecture text gives its invariants, proposals and
    // cases. A short tag without a hyphen (a letter and a number) is left to
    // the reader: it shares its form with evidence labels.
    label: 'internal work-stage identifier: design tag',
    pattern: new RegExp(
      '\\b' + '(?:I-P?|VK-|Z-|S-|WO-|RZ-K|E-|EV-|SD-)' + '\\d+\\b'
      + '|\\b' + 'GW' + '\\d+\\b'
      + '|(?<![\\w-])' + 'ÄP-' + '\\d+\\b'
      // One or two digits only: a longer number after these letters is a
      // contract or standard number in product text.
      + '|\\b' + 'RV-' + '\\d{1,2}\\b(?!-)',
    ),
  },
  {
    // A docstring or a comment that opens with such a tag and a colon names
    // what it was written for, not what the code does. A comment is one that
    // follows whitespace (its own line or after code), in any of the comment
    // forms of the tracked languages; a heading or a list item starts at the
    // line's first column and is left alone. A label the product gives its
    // own sources, or a technical name of the same shape, is no such tag.
    label: 'internal work-stage identifier: leading tag',
    pattern: new RegExp(
      '(?:"""|\\s#\\s|//\\s|^\\s+\\*\\s|/\\*+\\s)'
      + '(?!' + PRODUCT_NAMES_OF_TAG_SHAPE + ':)'
      + '[A-Z]{1,3}-?\\d{1,3}[a-z]?'
      + '(?:[,/]\\s?[A-Z]{1,3}-?\\d{1,3}[a-z]?)*' + ':\\s',
    ),
  },
] as const

// Notes about how a behaviour was observed, which instruction someone gave,
// how a past mutation run scored, what a past run measured, or which change
// fixed it. A reader needs the behaviour and its reason, not the history of
// finding it. Each form is precise enough to leave product text alone and is
// proven below like the identifiers above.
const WORK_NOTE_FORMS = [
  {
    label: 'internal work note: observation note',
    pattern: new RegExp(
      '\\b' + '(?:seen|observed)' + '(?: by a reader)?\\s+'
      + '(?:live\\b(?!\\s+by\\b)|in a live (?:run|instance)\\b)',
      'i',
    ),
  },
  {
    label: 'internal work note: operator instruction',
    pattern: new RegExp(
      '\\b' + 'standing' + '\\s+operator\\s+rule\\b'
      + '|\\b' + "operator'?s" + '\\s+explicit\\s+choice\\b',
      'i',
    ),
  },
  {
    label: 'internal work note: mutation tally',
    pattern: new RegExp(
      '\\b' + 'green under' + '\\s+(?:exactly\\s+)?(?:that|this|the same)\\s+mutation\\b',
      'i',
    ),
  },
  {
    label: 'internal work note: review observation',
    pattern: new RegExp('\\b' + 'seen in' + '\\s+review\\b', 'i'),
  },
  {
    label: 'internal work note: past measurement',
    pattern: new RegExp(
      '\\b' + 'measured' + '\\s+(?:on|against)\\s+(?:a\\s+|the\\s+)?(?:real|live)\\s+'
      + '(?:\\w+\\s+)?(?:run|table|build|server|stack|instance)\\b',
      'i',
    ),
  },
  {
    // A measurement told with the day it was taken belongs to the work, not
    // to the text: the day says nothing a reader can use.
    label: 'internal work note: dated measurement',
    pattern: new RegExp(
      '\\b' + '(?:replayed|re-?ran|measured|counted|recounted)' + '\\s+(?:on|against|over|across|with)\\b'
      + '[^.]{0,120}?\\b' + '(?:\\d{1,2}\\.\\d{1,2}\\.20\\d{2}|20\\d{2}-\\d{2}-\\d{2})' + '\\b',
      'i',
    ),
  },
  {
    label: 'internal work note: change reference',
    pattern: new RegExp(
      '\\b' + 'this change' + '\\s+(?:fixed|introduced|added|removed)\\b'
      + '|\\b' + 'before this' + '\\s+fix\\b'
      + '|\\b' + 'the review of' + '\\s+this\\s+change\\b',
      'i',
    ),
  },
] as const

const SOURCE_MARKERS = [
  ...FORBIDDEN_SOURCE_MARKERS,
  ...WORK_STAGE_IDENTIFIERS,
  ...WORK_NOTE_FORMS,
] as const

function markerLabels(line: string): string[] {
  return SOURCE_MARKERS
    .filter((marker) => marker.pattern.test(line))
    .map((marker) => marker.label)
}

const DOCSTRING_QUOTES = '"""'
const DOCSTRING_QUOTES_ALONE = new RegExp('^\\s*[rRbBuU]{0,2}' + DOCSTRING_QUOTES + '\\s*$')

// Every marked line of one file. A Python docstring may start its text on
// the line after the opening quotes; that line is read as if it followed the
// quotes directly, so a tag that opens the docstring is found there too.
function sourceViolations(relativePath: string, source: string): string[] {
  const violations: string[] = []
  const lines = source.split(/\r?\n/)
  const python = relativePath.endsWith('.py')
  let insideDocstring = false
  for (const [index, line] of lines.entries()) {
    for (const label of markerLabels(line)) {
      violations.push(`${relativePath}:${index + 1}: ${label}: ${line.trim()}`)
    }
    const next = lines[index + 1]
    if (python && !insideDocstring && next !== undefined && DOCSTRING_QUOTES_ALONE.test(line)) {
      for (const label of markerLabels(DOCSTRING_QUOTES + next.trimStart())) {
        if (!markerLabels(next).includes(label)) {
          violations.push(`${relativePath}:${index + 2}: ${label}: ${next.trim()}`)
        }
      }
    }
    if (python && (line.split(DOCSTRING_QUOTES).length - 1) % 2 === 1) {
      insideDocstring = !insideDocstring
    }
  }
  return violations
}

describe('repository version-control boundary', () => {
  test('keeps every private agent-memory path untracked', () => {
    const tracked = git(['ls-files', '-z'])
      .split('\0')
      .filter(Boolean)
      .filter(isPrivateAgentPath)
    assert.deepEqual(
      tracked,
      [],
      `Private agent-memory paths are tracked:\n${tracked.join('\n')}`,
    )
  })

  test('keeps representative spelling variants ignored', () => {
    const result = spawnSync(
      'git',
      ['check-ignore', '--no-index', '--stdin'],
      {
        cwd: REPOSITORY_ROOT,
        encoding: 'utf8',
        input: `${PRIVATE_PATH_EXAMPLES.join('\n')}\n`,
        stdio: ['pipe', 'pipe', 'pipe'],
      },
    )
    assert.equal(
      result.status,
      0,
      `git check-ignore failed: ${result.stderr.trim()}`,
    )
    assert.deepEqual(
      new Set(result.stdout.trim().split('\n').filter(Boolean)),
      new Set(PRIVATE_PATH_EXAMPLES),
    )
  })
})

describe('timeless versioned source', () => {
  test('contains no known private plan, incident, or chronology markers', () => {
    const violations: string[] = []
    for (const relativePath of trackedTextPaths()) {
      const source = readFileSync(resolve(REPOSITORY_ROOT, relativePath), 'utf8')
      violations.push(...sourceViolations(relativePath, source))
    }
    assert.deepEqual(
      violations,
      [],
      `Versioned source contains private chronology:\n${violations.join('\n')}`,
    )
  })

  test('binds every eval baseline to the current Inqtrix version', () => {
    const baselineDirectory = resolve(REPOSITORY_ROOT, 'tests/eval/baselines')
    const expectedVersion = readInqtrixVersion()
    const violations: string[] = []
    for (const name of readdirSync(baselineDirectory).filter(
      (entry) => entry.endsWith('.json'),
    )) {
      const baseline = JSON.parse(
        readFileSync(resolve(baselineDirectory, name), 'utf8'),
      ) as Record<string, unknown>
      if (baseline._inqtrix_version !== expectedVersion) {
        violations.push(
          `${name}: _inqtrix_version must be ${expectedVersion}`,
        )
      }
      if (
        typeof baseline._context !== 'string'
        || baseline._context.trim().length === 0
      ) {
        violations.push(`${name}: _context must be a non-empty string`)
      }
      if ('_established' in baseline) {
        violations.push(`${name}: _established is not permitted`)
      }
    }
    assert.deepEqual(violations, [], violations.join('\n'))
  })
})

describe('internal work-stage identifier patterns', () => {
  // Every planted line is assembled at runtime, so this file's own source
  // never carries the form it plants and the repository scan stays green.
  const planted = (...parts: string[]) => parts.join('')
  const PLANTED_LINES: Record<string, string[]> = {
    'dotted stage': [
      planted('# -- ', 'N7', '.4c', ': the ledger keeps its order'),
      planted('whatever ', 'N12', '.3', ' introduced'),
    ],
    'lettered stage': [
      planted('# -- follow-up to ', '3b', '.2c', ': one owner'),
      planted('# ', '2D', '.7', ' - the queue guard'),
    ],
    'lettered phase': [
      planted('the same shortcut as in ', 'Phase', ' 6c'),
      planted('added by the ', 'phase', '-8d', ' cleanup'),
    ],
    'lettered milestone': [
      planted('Session archive service (', 'M9', 'b storage tier).'),
    ],
    'run letter': [
      planted('Der ', 'Lauf', ' Q', '-Fehler: der Satz nannte keine Quelle.'),
      planted('(', 'Lauf', ' 4', ')'),
    ],
    'numbered counter-case': [
      planted('"""', 'Counter', '-case', ' 31: the shared anchor."""'),
      planted('siehe ', 'Gegen', 'fall', ' 17c'),
    ],
    'finding id': [
      planted('three stale rows stayed visible (', 'F-', 'Q7-11', ')'),
      planted('the ', 'HOLD-', 'F-', '52', ' exposure'),
    ],
    'numbered finding': [
      planted('describe("Neben', 'befund', ' #', '4: hover callbacks")'),
    ],
    'review id': [
      planted('must stay sorted, ', 'review', ' K7', '-K2).'),
    ],
    'decision id': [
      planted('does not rank it (', 'E-', 'Q4-2', ').'),
      planted('symmetry (', 'P5-', 'E8', ')'),
    ],
    'numbered decision': [
      planted('empty input always passes, ', 'decision', ' #', '14'),
    ],
    'register id': [
      planted('listed as ', 'R-', '417', ' in the backlog'),
    ],
    'invariant tag': [
      planted('keeps ', 'invariant', ' I', '9', ' across restarts'),
    ],
    'plan package': [
      planted('The single retry path (', 'plan', ' D7', ').'),
      planted('storage adapters (', 'Paket', ' F2', ').'),
    ],
    'priority stage': [
      planted('Warm the cache (', 'P', '14', ', eager).'),
      planted('the fallback order (', 'P', '2.7', ').'),
      planted('Session grants ', 'P', '8C', ' apply only there'),
      planted('/* ', 'P', '11b', ': queued anchors */'),
    ],
    'work-package id': [
      planted('"""', 'WP-', 'Q-R', ': archive retention routes."""'),
      planted('# ', 'WP-', 'X-Y', '/Z: quota headers'),
    ],
    'operator decision id': [
      planted('kept as decided (', 'O-', '412', ').'),
      planted('# ', 'O-', '7', ': the cap stays at two'),
    ],
    'plan phase id': [
      planted('moves to the runtime package with ', 'AU-', '41', '.'),
      planted('the fixture seam of ', 'AU-', '09', 'b'),
    ],
    'design tag': [
      planted('Always visible (', 'I-', '31', ').'),
      planted('the same reading as ', 'I-P', '7'),
      planted('per line (', 'VK-', '9', '): the source'),
      planted('"""', 'Z-', '14', ' holds for the point check."""'),
      planted('names the flag (', 'S-', '6', ')'),
      planted('assigned by ', 'WO-', '22'),
      planted('the case ', 'GW', '17', ' restores the fence'),
      planted('consistency note ', 'RZ-K', '3'),
      planted('as the proposal ', 'E-', '12', ' has it'),
      planted('keeps the excerpt (', 'EV-', '4', ')'),
      planted('the surface rule ', 'SD-', '2'),
      planted('the change point ', 'ÄP-', '7', ' moved'),
      planted('closes the review finding ', 'RV-', '12'),
    ],
    'leading tag': [
      planted('    """', 'G', '4', ': a primary source with no text.'),
      planted('"""', 'F', '4', '/', 'I', '3', ': whatever the evidence layer ties to a source'),
      planted('"""', 'VK', '5', ', ', 'I', '10', ': the cut is visible everywhere'),
      planted('    # ', 'I', '3', ': every statement the evidence layer ties to a source'),
      planted('    // ', 'I', '3', ': every statement the evidence layer ties to a source'),
      planted(' * ', 'VK', '5', ': the cut is visible everywhere'),
      planted('  /* ', 'G', '4', ': a primary source with no text. */'),
      planted('  /** ', 'G', '4', ': a primary source with no text.'),
      planted('return rows  # ', 'I', '10', ': the cut is visible everywhere'),
      planted('const kept = rows  // ', 'F', '4', ': tied to a source'),
      planted('    # ', 'K', '1', ', ', 'I', '3', ': a source label next to a tag'),
    ],
  }

  // Product text that shares a surface with the forms above and must pass.
  const LEGITIMATE_LINES = [
    'Der delegierte Lauf möchte den Plan erweitern (v{version}): {reason}',
    'Dieser Lauf wurde abgebrochen.',
    'Der Betreiber hat dieses Limit festgelegt.',
    'the event carries phase=planning while the plan is drafted',
    'Beleg „Anlage A, Fassung 2“ fehlt.',
    'Die Frist beträgt 30 Tage [K16] [E3] [W7], siehe Befund [F1].',
    'RECHERCHE-ERGEBNIS R1',
    'def do_GET(self) -> None:  # noqa: N802 - http.server contract',
    '<td className="border-r border-border last:border-r-0" />',
    '<path d="M3.714 3.048a.498.498 0 0 0-.683.627l18-8.5a.5.5 0 0 0 0-.904z" />',
    '### Stage 2a -- Contradictions cap',
    'subgraph p1 ["Phase 1: provider search"]',
    'display_name="BGE-M3",',
    '"answer": "DeepSeek R1 caused a stock crash in China.",',
    'Fuer Stoerungen der Prioritaet P1 (Totalausfall); P2-Stoerungen',
    '| **Object store (S3)** | Shared file storage across replicas |',
    "{ marker: 'R10', context: 'knowledge' }",
    '#   E9 - syntax and IO errors',
    '`round` is the review round (`0` without a review).',
    'Pricing tier: ``Standard S0``',
    'The contract in one place: every finding is applied or named.',
    'https://example.org/wp-content/uploads/2026/05/report.pdf',
    'Section WP-7 of the tender lists two lots.',
    'Timestamps follow ISO-8601; the transport needs TLS-1.3.',
    'a relative tolerance of 1E-10, a delay of 2.5e-05 seconds',
    'region us-east-1, the PKCE method S256 and the curve P-256',
    'Pumpe P-01 und Vertrag RV-2024-117',
    '<div className="relative z-10 gap-2" />',
    '# K1: Quelle aus der Wissenssammlung',
    '* W2: second web source of the answer',
    '    """Return the label K3: the third knowledge source."""',
    'headers=token(U2))',
    '    # K1: Quelle aus der Wissenssammlung',
    '        # W12: the web source the sentence cites',
    '    """K2: the second knowledge source of the answer."""',
    '  // K1: first knowledge source of the fixture',
    '    # S3: bucket and prefix of the object store',
    'digest = sha256(blob)  # SHA-256: 32 bytes, hex encoded',
    ' * UTF-8: the only encoding the export writes',
    '  K1: {',
    "      R6: { kind: 'knowledge_document', label: 'knowledge original' },",
    'E-Mail, E-Bike und E-Scooter',
    'SD-Karte, EV-Batterie und die Verträge RV-2024-117-A und RV-24-117',
  ]

  test('flags each form of an internal work-stage identifier', () => {
    assertEveryFormFlagged(
      WORK_STAGE_IDENTIFIERS,
      'internal work-stage identifier: ',
      PLANTED_LINES,
    )
  })

  test('lets legitimate product text pass', () => {
    assertNothingFlagged(LEGITIMATE_LINES)
  })

  test('finds a tag that opens a docstring on the line after the quotes', () => {
    const quotes = planted('"', '"', '"')
    const tagged = planted('    ', 'I', '3', ': every statement is tied to a source.')
    const source = [
      'def cut(rows):',
      `    ${quotes}`,
      tagged,
      `    ${quotes}`,
      '    return rows',
    ].join('\n')
    assert.deepEqual(sourceViolations('sample.py', source), [
      `sample.py:3: internal work-stage identifier: leading tag: ${tagged.trim()}`,
    ])
    // The same text in another language is no docstring.
    assert.deepEqual(sourceViolations('sample.ts', source), [])
    // A form the line carries on its own is reported once, on that line.
    const listed = planted('    listed as ', 'R-', '417', ' in the backlog')
    assert.deepEqual(
      sourceViolations('sample.py', ['def cut(rows):', `    ${quotes}`, listed, `    ${quotes}`].join('\n')),
      [`sample.py:3: internal work-stage identifier: register id: ${listed.trim()}`],
    )
  })

  test('leaves a constant of the same shape after closing quotes alone', () => {
    const quotes = planted('"', '"', '"')
    const source = [
      'class Limits:',
      `    ${quotes}`,
      '    Limits of one run.',
      `    ${quotes}`,
      '    T1: int = 1',
      `    ${quotes}Second docstring on one line.${quotes}`,
      '    P2: int = 2',
    ].join('\n')
    assert.deepEqual(sourceViolations('sample.py', source), [])
  })
})

describe('internal work note patterns', () => {
  // Assembled at runtime for the same reason as the identifier plants.
  const planted = (...parts: string[]) => parts.join('')
  const PLANTED_LINES: Record<string, string[]> = {
    'observation note': [
      planted('    // ', 'Seen', ' live', ': two toolbar rows before the answer.'),
      planted('skipped 5 of 12 files (', 'seen', ' live', ' on a Deep run).'),
      planted('the limit text was ', 'seen', ' by a reader', ' in a live', ' instance.'),
      planted('    ', 'Observed', ' live', ': the counter jumped back once.'),
    ],
    'operator instruction': [
      planted('', 'Standing', ' operator rule', ': every retry is logged.'),
      planted(' * 250ms is the ', "operator's", ' explicit choice', ' — quieter than the default'),
    ],
    'mutation tally': [
      planted('(41 respectively 9 ', 'green under', ' exactly that mutation).'),
      planted('(all 802 other tests stayed ', 'green under', ' the same mutation)'),
    ],
    'review observation': [
      planted('    // ', 'Seen', ' in review', ': the badge showed a stale count.'),
      planted('a gap ', 'seen in', ' review', ' on the export path'),
    ],
    'past measurement': [
      planted('', 'Measured on', ' a real', ' run, the sync took 3:05.'),
      planted('', 'Measured against', ' the live', ' table it matched 2 of 9 rows.'),
      planted('// ', 'Measured on', ' a real', ' completed run: the list read 1/4.'),
    ],
    'dated measurement': [
      planted('', 'Replayed on', ' the extraction inputs of six runs of ', '17.10.', '2026', ': 300 of 393 placeable.'),
      planted('', 'Measured over', ' the stored runs of ', '2026-', '10-17', ', the guard held.'),
      planted('# ', 'counted against', ' the trace store on ', '3.11.', '2026'),
    ],
    'change reference': [
      planted('the very defect ', 'this change', ' fixed', '.'),
      planted('', 'Before this', ' fix', ' the list double-counted.'),
      planted('# -- what ', 'the review of', ' this change', ' found ----'),
      planted('the guard ', 'this change', ' introduced', ' stays.'),
    ],
  }

  // Product text that shares words with the forms above and must pass.
  const LEGITIMATE_LINES = [
    'Edits are seen live by every participant in the document.',
    'A live run streams answer tokens and appends step lines.',
    "operator's explicit defaults *and* the caller's explicit overrides",
    'the operator asked for observability, and the process exports it',
    'Mutation: dropping the guard turns this test red.',
    'The pin stays green when unrelated code changes.',
    'Items seen in the review panel stay selected.',
    'The width is measured on the rendered element.',
    'Latency is measured against the live clock of the host.',
    'This change cannot be undone.',
    'No valid decision is available for this change.',
    'The review of each draft happens before publishing.',
    'A fix proposed by the review applies to this passage only.',
    'Evidence labels in the quote that this fix removes.',
    'Applying this patch removes the paragraph.',
    'The text before this change is kept as a revision.',
    '// Synchronous first application, before this commit paints.',
    'Usage is counted across all runs of a month.',
    'Tokens are counted with the tokenizer of the model. Prices as listed on 2026-10-01.',
    'Replayed on the next start, the journal restores every pending write.',
  ]

  test('flags each form of an internal work note', () => {
    assertEveryFormFlagged(WORK_NOTE_FORMS, 'internal work note: ', PLANTED_LINES)
  })

  test('lets legitimate product text pass', () => {
    assertNothingFlagged(LEGITIMATE_LINES)
  })
})

function assertEveryFormFlagged(
  markers: readonly { label: string }[],
  prefix: string,
  plantedLines: Record<string, string[]>,
): void {
  const forms = markers.map((marker) => marker.label.replace(prefix, ''))
  assert.deepEqual(
    [...forms].sort(),
    Object.keys(plantedLines).sort(),
    'every pattern needs planted lines, and every planted form a pattern',
  )
  for (const [form, lines] of Object.entries(plantedLines)) {
    for (const line of lines) {
      assert.ok(
        markerLabels(line).includes(`${prefix}${form}`),
        `${form} is not flagged in: ${line}`,
      )
    }
  }
}

function assertNothingFlagged(lines: readonly string[]): void {
  const flagged = lines
    .map((line) => ({ line, labels: markerLabels(line) }))
    .filter(({ labels }) => labels.length > 0)
  assert.deepEqual(flagged, [])
}

function trackedTextPaths(): string[] {
  return git(['ls-files', '-z'])
    .split('\0')
    .filter(Boolean)
    .filter((relativePath) => (
      relativePath !== 'package-lock.json'
      && relativePath !== 'uv.lock'
      && !relativePath.startsWith('docs/development/archived-github-actions/')
    ))
    .filter((relativePath) => {
      const content = readFileSync(resolve(REPOSITORY_ROOT, relativePath))
      return !content.includes(0)
    })
}

describe('a scenario reports a measurement, not a default', () => {
  // A live engine that starts every scenario as "failed" reports twelve red
  // verdicts when it reached none of them: the column looks permanently
  // broken, carries no information and hides whichever failure is real.
  // The report layer keeps an unmeasured scenario not_run and leaves the
  // verdict to the engine's own status, so the engines must hand it
  // "not_run" rather than a pre-cooked verdict.
  const ENGINES = readdirSync(resolve(REPOSITORY_ROOT, 'e2e/engines'))
    .filter((name) => name.endsWith('.mjs'))

  for (const engine of ENGINES) {
    test(`${engine} seeds scenario status as not_run`, () => {
      const source = readFileSync(
        resolve(REPOSITORY_ROOT, 'e2e/engines', engine),
        'utf8',
      )
      const seeds = [...source.matchAll(/\[\s*id\s*,\s*'(\w+)'\s*\]/g)]
        .map((match) => match[1])
      const fallbacks = [...source.matchAll(/scenarioStatus\.get\([^)]*\)\s*\?\?\s*'(\w+)'/g)]
        .map((match) => match[1])
      for (const value of [...seeds, ...fallbacks]) {
        assert.notEqual(
          value,
          'failed',
          `${engine} books an unmeasured scenario as failed; use 'not_run'.`,
        )
      }
    })
  }
})

describe('one writer for the scenario results', () => {
  // Five copies of the result writer drifted apart from the reader: engines
  // wrote not_run, the reader threw the whole file away, and the report
  // blamed a scenario that had passed. The contract lives in one module, and
  // only it and the orchestrator touch the results path.
  const ALLOWED = new Set([
    'tests/verification/command-runner.ts',
    'tests/verification/fixtures/scenario-sidecar.mjs',
    // Keeps its load child from writing a second result file.
    'tests/verification/engines/collaboration-load-smoke.mjs',
  ])

  test('no engine writes the result file on its own', () => {
    const writers = trackedTextPaths()
      .filter((relativePath) => /\.(?:c|m)?(?:js|ts)$/.test(relativePath))
      .filter((relativePath) => !/\.test\.(?:c|m)?(?:js|ts)$/.test(relativePath))
      .filter((relativePath) => !ALLOWED.has(relativePath))
      .filter((relativePath) => readFileSync(resolve(REPOSITORY_ROOT, relativePath), 'utf8')
        .includes('INQTRIX_VERIFICATION_SCENARIO_RESULTS_PATH'))
    assert.deepEqual(writers, [])
  })
})

function isPrivateAgentPath(relativePath: string): boolean {
  const normalized = relativePath.replaceAll('\\', '/').toLowerCase()
  return (
    normalized.startsWith('.cursor/')
    || normalized.startsWith('.claude/')
    || ['agents.md', 'claude.md', 'cursor.md'].includes(basename(normalized))
  )
}

function git(args: readonly string[]): string {
  const result = spawnSync('git', [...args], {
    cwd: REPOSITORY_ROOT,
    encoding: 'utf8',
    stdio: ['ignore', 'pipe', 'pipe'],
  })
  assert.equal(
    result.status,
    0,
    `git ${args.join(' ')} failed: ${result.stderr.trim()}`,
  )
  return result.stdout
}

test('no live runner swallows a cleanup call silently', () => {
  const call = String.raw`\((?:[^()]|\((?:[^()]|\([^()]*\))*\))*\)`
  const swallowed = new RegExp(
    String.raw`(?:logoutActor|logoutContext|eraseTemporaryUser|deleteCollaborationDocument|revokeGuestLink|lifecycle\.complete)`
    + call
    + String.raw`\s*(?:\.then` + call + String.raw`\s*)?\.catch\(\(\) =>`,
  )
  const offenders = trackedTextPaths()
    .filter((path) => /^(?:e2e|tests\/verification)\/.*\.(?:mjs|ts)$/.test(path))
    .filter((path) => !/\.test\./.test(path))
    .filter((path) => swallowed.test(readFileSync(resolve(REPOSITORY_ROOT, path), 'utf8')))

  assert.deepEqual(offenders, [])
})
