import remarkGfm from 'remark-gfm'
import remarkMath from 'remark-math'
import remarkParse from 'remark-parse'
import { unified } from 'unified'
import { describe, expect, it } from 'vitest'
import { createSeedProjectState } from './seedProject'

type MarkdownTreeNode = {
  children?: MarkdownTreeNode[]
  type?: string
}

function parsedTableWidths(markdown: string): number[][] {
  const tree = unified()
    .use(remarkParse)
    .use(remarkGfm)
    .use(remarkMath)
    .parse(markdown) as MarkdownTreeNode
  const pending = [tree]
  const tableWidths: number[][] = []

  while (pending.length > 0) {
    const node = pending.pop()
    if (!node?.children) continue

    if (node.type === 'table') {
      tableWidths.push(node.children.map((row) => row.children?.length ?? 0))
    }
    pending.push(...node.children)
  }

  return tableWidths
}

describe('createSeedProjectState database demo seed', () => {
  it('models EU law as a large grouped collection and vector index', () => {
    const state = createSeedProjectState()
    const legalSection = Object.values(state.fileLibrarySections).find(
      (section) => section.title === 'Rechtliche Grundlagen',
    )
    if (!legalSection) throw new Error('Expected the legal demo collection to exist.')

    const legalAssets = Object.values(state.fileAssets).filter(
      (asset) => asset.sectionId === legalSection.id,
    )
    expect(legalAssets).toHaveLength(50)

    const legalGroups = Object.values(state.fileGroups).filter(
      (group) => group.sectionId === legalSection.id,
    )
    const legalGroupIds = new Set(legalGroups.map((group) => group.id))
    const groupIdsWithFiles = new Set(
      legalAssets
        .filter((asset) => asset.groupId && legalGroupIds.has(asset.groupId))
        .map((asset) => asset.groupId),
    )
    expect(groupIdsWithFiles.size).toBeGreaterThanOrEqual(4)
    expect(legalAssets.some((asset) => asset.groupId === null)).toBe(true)

    const euLawIndex = state.vectorIndexes['vector-index-eu-recht']
    if (!euLawIndex) throw new Error('Expected the EU law vector index to exist.')
    expect(euLawIndex.members.length).toBeGreaterThanOrEqual(50)
    expect(euLawIndex.members.length).toBeLessThanOrEqual(55)

    const assetIds = new Set(Object.keys(state.fileAssets))
    expect(euLawIndex.members.every((member) => assetIds.has(member.fileId))).toBe(true)

    const pendingMembers = euLawIndex.members.filter((member) => member.state === 'pending')
    expect(pendingMembers).toEqual([{ fileId: 'file-asset-rechtsgutachten', state: 'pending' }])
    expect(state.indexingJobs[euLawIndex.id]?.runningFileIds).toEqual(['file-asset-rechtsgutachten'])
  })
})

describe('createSeedProjectState research demo seed', () => {
  it('shows every bundled report with the basis line and the ceiling the server now records', () => {
    const runs = Object.values(createSeedProjectState().researchRuns).filter((run) => run.source === 'imported')
    expect(runs).toHaveLength(5)
    for (const run of runs) {
      // Each report rests on provider context and is clean, held at 6 by the
      // depth gap alone: no contract step, no warning notice, no single-source
      // piece in the footer.
      expect(run.result?.metrics).toMatchObject({
        confidence_cap: 6,
        confidence_cap_reasons: ['no_cross_checked_claims', 'majority_single_source_claims'],
        evidence_basis: 'provider_context',
        evidence_contract_causes: [],
        evidence_contract_status: 'clean',
      })
      const markdown = run.result?.markdown ?? ''
      expect(markdown).toMatch(
        /\n\nGrundlage: die Angaben des Suchanbieters zu den zitierten Quellen; die Seiten selbst wurden nicht gelesen\. Gestützte Aussagen der Recherche mit mehr als einer Quelle: \d+ von \d+\.\n\n---\n\*/,
      )
      expect(markdown).not.toContain('## Evidenzhinweis')
      expect(markdown).not.toContain('single-source (')
      const titles = run.events.map((event) => event.title)
      expect(titles).not.toContain(
        'Belege des Berichts mit Einschränkung — der Evidenzhinweis unter dem Bericht nennt den Grund',
      )
    }
  })

  it('splits the statements of every bundled report by the rule the server applies now', () => {
    // None of the five exports carries a search provider's excerpt, so every
    // statement they support is attributed by the search provider and none is
    // verified: in the counts, the step titles, the top statements, the footer
    // and the report text alike.
    const runs = Object.values(createSeedProjectState().researchRuns).filter((run) => run.source === 'imported')
    expect(runs).toHaveLength(5)
    for (const run of runs) {
      expect((run.result?.references ?? []).filter((reference) => reference.provider_snippet || reference.statements?.length))
        .toEqual([])
      const counts = run.result?.metrics?.claims?.status_counts ?? {}
      expect(counts.verified).toBe(0)
      expect(counts.attributed).toBeGreaterThan(0)
      expect(run.result?.metrics?.verified_claims_used_count).toBe(0)
      expect(run.snapshot?.claim_status_counts).toEqual(counts)
      const claims = run.result?.topClaims ?? []
      expect(claims.filter((claim) => claim.status === 'verified')).toEqual([])
      expect(claims.some((claim) => claim.status === 'attributed')).toBe(true)
      const steps = run.events.map((event) => event.title).filter((title) => title.startsWith('Aussagenlage: '))
      expect(steps.length).toBeGreaterThan(0)
      for (const title of steps) expect(title).toMatch(/^Aussagenlage: verifiziert 0, vom Anbieter zugeschrieben \d+, /)
      expect(steps.at(-1)).toContain(`vom Anbieter zugeschrieben ${counts.attributed}, nicht belegt ${counts.unverified};`)
      const markdown = run.result?.markdown ?? ''
      const footer = markdown.slice(markdown.lastIndexOf('\n---\n*'))
      expect(footer).not.toContain('verifizierte Aussage')
      expect(footer).toContain(`· ${counts.attributed} vom Anbieter zugeschriebene Aussagen ·`)
      // The report text counts the same statements by the same word.
      expect(markdown).not.toMatch(/verifizierten (Einzel)?[Aa]ussagen|verifizierten Belege|als verifiziert eingestuft/)
    }
  })

  it('words the steps of the running demo run like the server', () => {
    const running = Object.values(createSeedProjectState().researchRuns).filter((run) => run.status === 'running')
    expect(running).toHaveLength(1)
    const titles = running[0].events.map((event) => event.title)
    expect(titles.join('\n')).not.toMatch(/Pflichtaspekt|Durchsuche \d+ Anfragen/)
    // The server's forms: classify_subquestions and search_start.
    expect(titles.some((title) => /^Analyseziele erkannt: \d+ Teilfragen: '[^']+'(, '[^']+')*$/.test(title))).toBe(true)
    expect(titles.some((title) => /^Durchsuche \d+ Suchanfragen \(Runde \d+\/\d+\)\.\.\.$/.test(title))).toBe(true)
  })

  it('shows the server-written parts of every bundled report in the reader words the server writes now', () => {
    // The footer, the line and the step titles follow the server; the
    // report prose of the model stays as it was exported, except for the
    // counted status of its statements (see the split above).
    const runs = Object.values(createSeedProjectState().researchRuns).filter((run) => run.source === 'imported')
    expect(runs).toHaveLength(5)
    for (const run of runs) {
      const markdown = run.result?.markdown ?? ''
      const footer = markdown.slice(markdown.lastIndexOf('\n---\n*')).trimEnd()
      expect(footer).toMatch(/· Konfidenz \d+\/10\*$/)
      for (const code of ['Evidence-Contract', 'Evidence-Prompt', 'Confidence', 'ALGO-FAIL', 'im Report']) {
        expect(footer).not.toContain(code)
      }
      const titles = run.events.map((event) => event.title).join('\n')
      for (const code of [
        'Confidence', 'Evidenz-Records', 'Claim', 'STORM', 'DEEP', 'Cross-Check', 'ALGO-FAIL',
        'Report-Abschnitt', 'min_rounds', ' primary', ' mainstream', ' stakeholder', ' unknown', ' low;',
      ]) {
        expect(titles).not.toContain(code)
      }
      expect(titles).toMatch(/^Aussagenlage: verifiziert \d+, vom Anbieter zugeschrieben \d+, nicht belegt \d+; gegengeprüft, unabhängig vom Stand: \d+$/m)
    }
  })
})

describe('createSeedProjectState chat demo seed', () => {
  it('loads the long battery digital-twin renderer reference as the newest chat', () => {
    const state = createSeedProjectState()
    const thread = Object.values(state.chatThreads).find(
      (candidate) => candidate.title === 'Digitaler Zwilling eines Batteriespeichers',
    )
    if (!thread) throw new Error('Expected the battery digital-twin demo chat to exist.')

    expect(thread.messages).toHaveLength(6)
    expect(state.chatThreadGroupMemberships[thread.id]).toBe('chat-group-demos')
    expect(state.ui.selectedChatThreadId).toBe(thread.id)
    expect(state.ui.pinnedExplorer.chatThreadIds).toEqual([thread.id])

    const assistantMarkdown = thread.messages
      .filter((message) => message.role === 'assistant')
      .map((message) => message.contentMarkdown)
      .join('\n')
    const wordCount = assistantMarkdown.trim().split(/\s+/).length
    const tableSeparators = assistantMarkdown.match(/^\|(?:\s*:?-+:?\s*\|){2,}$/gm) ?? []
    const tableWidths = parsedTableWidths(assistantMarkdown)

    expect(wordCount).toBeGreaterThanOrEqual(2400)
    expect(assistantMarkdown.match(/```python\b/g) ?? []).toHaveLength(2)
    expect(assistantMarkdown.match(/```mermaid\b/g) ?? []).toHaveLength(3)
    expect((assistantMarkdown.match(/\$\$/g) ?? []).length).toBeGreaterThanOrEqual(24)
    expect(tableSeparators.length).toBeGreaterThanOrEqual(5)
    expect(tableWidths.length).toBeGreaterThanOrEqual(5)
    expect(tableWidths.every((rows) => rows.every((width) => width === rows[0]))).toBe(true)
  })

  it('seeds the rendering standard: one answer that exercises every construct the renderer promises', () => {
    const state = createSeedProjectState()
    const thread = Object.values(state.chatThreads).find(
      (candidate) => candidate.title === 'Darstellungsstandard',
    )
    if (!thread) throw new Error('Expected the rendering-standard demo chat to exist.')

    expect(thread.messages.map((message) => message.role)).toEqual(['user', 'assistant'])
    expect(state.chatThreadGroupMemberships[thread.id]).toBe('chat-group-demos')
    const markdown = thread.messages[1].contentMarkdown

    // The eight promised diagram grammars in the documented order, then the
    // paired plots (two adjacent fences share a row).
    const fences = [...markdown.matchAll(/```mermaid\n\s*([\w-]+)/g)].map((match) => match[1])
    expect(fences).toEqual([
      'flowchart', 'sequenceDiagram', 'classDiagram', 'stateDiagram-v2', 'erDiagram', 'gantt', 'pie', 'xychart-beta',
      'xychart-beta', 'xychart-beta',
    ])
    expect(markdown.match(/^\s*accTitle:/gm) ?? []).toHaveLength(10)
    expect(markdown).toMatch(/```\n\n```mermaid\nxychart-beta/)
    expect(markdown).not.toMatch(/%%\{init/)
    // Three code languages, three tables (two columns, nine columns, a
    // formula cell), inline and display math including an aligned block.
    expect([...markdown.matchAll(/```(python|sql|json)\b/g)].map((match) => match[1])).toEqual(['python', 'sql', 'json'])
    const tableWidths = parsedTableWidths(markdown)
    expect(tableWidths.map((rows) => rows[0]).sort((a, b) => a - b)).toEqual([2, 3, 9])
    expect(tableWidths.every((rows) => rows.every((width) => width === rows[0]))).toBe(true)
    expect(markdown).toMatch(/\|\s*\$\\eta_\{\\mathrm\{hyd\}\}/)
    expect(markdown.match(/\$\$/g) ?? []).toHaveLength(4)
    expect(markdown).toContain('\\begin{aligned}')
    // Currency is written as text, never as a dollar sign that KaTeX could pair.
    expect(markdown).toContain('USD 5')
    expect(markdown).not.toMatch(/\$\d/)
    // Blockquote, ordered and nested lists, task list, link, footnote — and
    // no horizontal rule: headings carry the structure of the standard.
    expect(markdown).toMatch(/^> /m)
    expect(markdown).toMatch(/^1\. /m)
    expect(markdown).toMatch(/^ {3}- /m)
    expect(markdown).toMatch(/^- \[x\] /m)
    expect(markdown.split('\n').slice(11)).not.toContain('---')
    expect(markdown).toMatch(/\[Wikipedia: Kreiselpumpe\]\(https:/)
    expect(markdown).toMatch(/^\[\^1\]: /m)
    // User-visible German carries real umlauts, never ae/oe/ue spellings.
    expect(markdown).not.toMatch(/Waehrung|Foerder|Stoerung|Laeuft|Pruef|\bueber|\bfuer\b|Groesse|Kuehl|Maerz|Ueber/)
  })
})
