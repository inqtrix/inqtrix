import { readFileSync } from 'node:fs'
import path from 'node:path'

import { renderToStaticMarkup } from 'react-dom/server'
import ts from 'typescript'
import { describe, expect, it, vi } from 'vitest'

import { TooltipProvider } from '@/components/ui/tooltip'
import { translations } from '@/i18n/translations'
import { AgentComposer } from './AgentComposer'

vi.mock('@/i18n/LocaleProvider', () => ({
  useLocale: () => ({ locale: 'de', t: translations.de }),
}))

const noop = () => {}

function composer(props: Partial<Parameters<typeof AgentComposer>[0]>) {
  return renderToStaticMarkup(
    <TooltipProvider>
      <AgentComposer
      autonomy="balanced"
      autonomyModes={['balanced']}
      collections={[{ id: 'kc_1', title: 'EU-AI-Act-vec' }]}
      draftQuestion=""
      onAutonomyChange={noop}
      onDraftQuestionChange={noop}
      onSelectedCollectionIdsChange={noop}
      onSourcePolicyChange={noop}
      onStop={noop}
      onSubmit={() => true}
      reportIdsMax={3}
      ruleIdsMax={3}
      selectedCollectionIds={[]}
      sourcePolicy={{ web: 'disabled', knowledge: 'available' }}
      {...props}
      />
    </TooltipProvider>,
  )
}

const glyph = (html: string) =>
  html.match(/data-source="knowledge" data-source-state="([a-z]+)"/)?.[1] ?? null

describe('agent composer knowledge switch', () => {
  it('shows no knowledge glyph until the session asked for project knowledge', () => {
    expect(glyph(composer({}))).toBeNull()
    expect(composer({})).toContain('data-source="web"')
  })

  // The ON state lives in the text, as an inline pill where the command was
  // typed, not in the toolbar. The glyph keeps the states the pill cannot
  // show — off and unavailable — because without it there is no way back on
  // except typing `/wissen`.
  //
  // Scope: this renders to static markup, so the TipTap document is not
  // mounted and the pill itself cannot appear here. What this pins is that
  // the toolbar does not claim the on state; the pill is covered by the
  // contract pins in executionPolicy.test.ts.
  it('does not claim the on state in the toolbar', () => {
    expect(glyph(composer({ knowledgeSwitch: 'all' }))).toBeNull()
  })

  it('keeps the glyph for the off state', () => {
    const off = composer({ knowledgeSwitch: 'off' })
    expect(glyph(off)).toBe('off')
    expect(off).toContain('agent-source-glyph-off')
    expect(off).not.toContain(translations.de.agent.composer.knowledgeChipRemove)
  })

  it('hides the glyph while a chip pins the scope', () => {
    expect(glyph(composer({ knowledgeSwitch: 'all', selectedCollectionIds: ['kc_1'] }))).toBeNull()
  })

  it('greys the switch out with the server reason while the store is unreachable', () => {
    const html = composer({
      knowledgeSwitch: 'all',
      sourceAvailability: { web: true, knowledge: false, knowledgeReason: 'store_unreachable' },
    })
    expect(glyph(html)).toBe('unavailable')
    expect(html).toContain(translations.de.agent.composer.sourceReasonStoreUnreachable)
    // A switched-on session may still be switched OFF while the store is
    // down (the server refuses the request otherwise); an off switch cannot
    // be switched on.
    const knowledgeButton = (markup: string) =>
      markup.match(/<button[^>]*data-source="knowledge"[^>]*>/)?.[0] ?? ''
    expect(knowledgeButton(html)).toContain('aria-disabled="false"')
    const off = composer({
      knowledgeSwitch: 'off',
      sourceAvailability: { web: true, knowledge: false, knowledgeReason: 'operator_disabled' },
    })
    expect(knowledgeButton(off)).toContain('aria-disabled="true"')
    expect(off).toContain(translations.de.agent.composer.sourceReasonOperatorDisabled)
  })

  it('shows a refused submission as an alert beside the draft', () => {
    const html = composer({ submitError: 'The knowledge store is not available; the run was not started.' })
    expect(html).toContain('data-testid="agent-submit-error"')
    expect(html).toContain('role="alert"')
    expect(html).toContain('Anfrage abgelehnt: The knowledge store is not available; the run was not started.')
    expect(composer({})).not.toContain('agent-submit-error')
  })

  it('does not mislabel a locked composer as unavailable', () => {
    const html = composer({ disabled: true, knowledgeSwitch: 'all' })
    expect(html).not.toContain(translations.de.agent.composer.sourceReasonOperatorDisabled)
    expect(html.match(/<button[^>]*data-source="web"[^>]*>/)?.[0]).toContain('data-source-state="off"')
  })
})

describe('a skill has ONE representation', () => {
  // `/wissen`, `/canvas` and `/skill` are CHOSEN with a slash and therefore
  // live inline in the text, while the chip row above the input belongs to
  // what `@` ATTACHES (reports, patch target, collections). A toolbar chip
  // for a slash-chosen skill would show that skill twice, once inline and
  // once above the input, so the chip must stay away.
  //
  // Scope: this renders to static markup, so TipTap is not mounted and the
  // inline capsule cannot appear here. What this pins is that the TOOLBAR
  // does not carry the skill; that the capsule does is covered by the
  // pill derivation.
  const skill = { argument_hint: '', description: 'd', id: 'sk_1', label: 'probe' }

  it('renders no skill chip beside the inline capsule', () => {
    const html = composer({ selectedSkillIds: ['sk_1'], slashSkills: [skill] })
    expect(html).not.toContain('/probe')
    expect(html).not.toContain('Skills: probe')
  })

  it('still renders the chips that @ attaches', () => {
    const html = composer({
      collections: [{ id: 'kc_1', title: 'EU-AI-Act-vec' }],
      selectedCollectionIds: ['kc_1'],
      selectedSkillIds: ['sk_1'],
      slashSkills: [skill],
    })
    expect(html).toContain('EU-AI-Act-vec')
  })
})

describe('what the "+" menu offers', () => {
  const addButton = (html: string) => {
    const label = translations.de.agent.composer.addContext
    return html.match(new RegExp(`<button[^>]*aria-label="${label}"[^>]*>`))?.[0] ?? ''
  }
  const briefing = {
    composedChars: 17_230, id: 'pt_brief', label: 'briefing', linkedFiles: false,
    notSynced: false, revision: 3, title: 'Executive Briefing',
  }
  const report = { label: 'co2', markdown: '# Bericht', runId: 'run_r1', title: 'CO2-Flottengrenzwerte' }

  // The attribute, not a `disabled:` style variant in the class list.
  const isDisabled = (button: string) => /\sdisabled=""/.test(button)

  it('opens for a rule alone', () => {
    const html = composer({ collections: [], onSelectedRuleIdsChange: noop, ruleOptions: [briefing] })
    expect(addButton(html)).not.toBe('')
    expect(isDisabled(addButton(html))).toBe(false)
    // Control: without a rule the menu has nothing to open.
    expect(isDisabled(addButton(composer({ collections: [] })))).toBe(true)
  })

  it('offers reports to the kernel engine only', () => {
    // The server refuses report_ids on a mission run, so the mission
    // engine must not offer them in the first place.
    const props = { collections: [], onReportIdsChange: noop, reportOptions: [report] }
    expect(isDisabled(addButton(composer({ ...props, engineMode: 'workspace_agent' })))).toBe(true)
    expect(isDisabled(addButton(composer({ ...props, engineMode: 'agent_kernel' })))).toBe(false)
  })
})

describe('the caps the composer pre-checks', () => {
  // Source pin (DOM-less lane: the menus open only in the mounted editor).
  // What a missing cap does is tested on the pure model and its words
  // (contextCategories.test.ts, contextCategoryText.test.ts); this pins
  // that the composer has no number of its own to fall back to — a default
  // here was a second copy of the server's cap that hid an unpublished one.
  const tree = ts.createSourceFile(
    'AgentComposer.tsx',
    readFileSync(path.resolve(__dirname, './AgentComposer.tsx'), 'utf-8'),
    ts.ScriptTarget.Latest,
    true,
  )
  function propBinding(prop: string): string | null {
    let found: string | null = null
    function visit(node: ts.Node) {
      if (ts.isFunctionDeclaration(node) && node.name?.getText(tree) === 'AgentComposer') {
        const [props] = node.parameters
        if (props && ts.isObjectBindingPattern(props.name)) {
          for (const element of props.name.elements) {
            if ((element.propertyName ?? element.name).getText(tree) === prop) {
              found = element.getText(tree)
            }
          }
        }
        return
      }
      ts.forEachChild(node, visit)
    }
    visit(tree)
    return found
  }

  it('takes the report and the rule cap from its caller, with no default', () => {
    expect(propBinding('reportIdsMax')).toBe('reportIdsMax: reportLimit')
    expect(propBinding('ruleIdsMax')).toBe('ruleIdsMax: ruleLimit')
  })
})

describe('attachments the selected engine refuses', () => {
  // Reports and canvas context feed the kernel only; the server refuses
  // them on a mission run with 400. Attached under the kernel and then
  // switched, the attachments stayed and the send failed — flagged before
  // it now, and held: the notice here, the warning on the capsule itself
  // (pinned in CommandPillView.test, where the capsule renders).
  const report = { label: 'co2', markdown: '# Bericht', runId: 'run_r1', title: 'CO2-Flottengrenzwerte' }
  const notice = translations.de.agent.composer.kernelOnlyAttached
    .replace('{engine}', translations.de.agent.composer.engineAuto)
  const noticeElement = (html: string) =>
    html.match(/<div[^>]*data-testid="agent-engine-refusal"[^>]*>/)?.[0] ?? ''

  it('flags an attached report before a mission send', () => {
    const html = composer({
      engineMode: 'workspace_agent', onReportIdsChange: noop, reportIds: ['run_r1'], reportOptions: [report],
    })
    expect(html).toContain(notice)
    // The capsules are described by this notice: it carries the id they name.
    expect(noticeElement(html)).toMatch(/ id="[^"]+"/)
  })

  it('flags a pinned canvas document the same way', () => {
    const html = composer({
      canvasDocuments: [{ artifactId: 'a1', name: 'memo.md', revision: 1, title: 'Memo' }],
      engineMode: 'workspace_agent',
      pinnedCanvasDocumentId: 'a1',
    })
    expect(html).toContain(notice)
  })

  it('says nothing under the kernel engine', () => {
    const html = composer({
      engineMode: 'agent_kernel', onReportIdsChange: noop, reportIds: ['run_r1'], reportOptions: [report],
    })
    expect(html).not.toContain(notice)
  })

  it('holds the send until the engine is switched or the attachment detached', () => {
    // The send went out anyway and came back as the server's 400.
    const send = (html: string) => html.match(/<button[^>]*data-testid="agent-submit"[^>]*>/)?.[0] ?? ''
    // A report the desk knows: only the engine can hold this send.
    const draft = {
      draftQuestion: 'Vergleiche bitte',
      onReportIdsChange: noop,
      reportCatalog: [report],
      reportIds: ['run_r1'],
      reportOptions: [report],
    }
    const mission = composer({ ...draft, engineMode: 'workspace_agent' })
    expect(mission).toContain(notice)
    expect(mission).not.toContain(translations.de.agent.composer.unavailableAttached)
    expect(send(mission)).toContain('disabled=""')
    // The same draft sends under the kernel engine.
    expect(send(composer({ ...draft, engineMode: 'agent_kernel' }))).not.toContain('disabled=""')
  })
})

describe('an answer to a clarification and the rule pills', () => {
  // Source pin (DOM-less lane: the TipTap document is not mounted here).
  // The pure halves — what the text spells, what the cleared draft keeps —
  // are tested in AgentInlineInput.test.ts; this pins that the submit uses
  // the ONE answer-mode set for both, so the rule neither lands in the
  // answer text nor leaves the draft with it.
  const tree = ts.createSourceFile(
    'AgentComposer.tsx',
    readFileSync(path.resolve(__dirname, './AgentComposer.tsx'), 'utf-8'),
    ts.ScriptTarget.Latest,
    true,
  )
  function callsOf(callee: string): string[] {
    const found: string[] = []
    function visit(node: ts.Node) {
      if (ts.isCallExpression(node) && node.expression.getText(tree) === callee) {
        found.push(node.getText(tree).replace(/\s+/g, ' '))
      }
      ts.forEachChild(node, visit)
    }
    visit(tree)
    return found
  }

  it('spells and clears through the answer-mode pill set', () => {
    expect(callsOf('pillKindsKeptByAnswer')).toEqual(['pillKindsKeptByAnswer(answerMode)'])
    expect(callsOf('inputRef.current?.textWithPillLabels')).toEqual([
      'inputRef.current?.textWithPillLabels(keptByAnswer, quote)',
    ])
    // What a send leaves: what an answer holds back, and after a task the
    // editing target.
    expect(callsOf('inputRef.current?.clear')).toEqual([
      'inputRef.current?.clear(pillKindsKeptAfterSend(answerMode))',
    ])
  })
})

describe('the "@" menu marks a row from what picking it does', () => {
  // Source pin (DOM-less lane: the menu opens on a caret in the mounted
  // editor only). Which row has which action is tested on the pure helper
  // (`agentMentionPick` in contextCategories.test.ts); this pins that the
  // composer marks a row as not pickable from THAT answer, and that click
  // and Enter use it too. The answer-mode state row once carried no mark
  // and showed the Enter hint for a pick that did nothing.
  const tree = ts.createSourceFile(
    'AgentComposer.tsx',
    readFileSync(path.resolve(__dirname, './AgentComposer.tsx'), 'utf-8'),
    ts.ScriptTarget.Latest,
    true,
  )
  function declaration(name: string): ts.Node | undefined {
    let found: ts.Node | undefined
    function visit(node: ts.Node) {
      if (
        (ts.isVariableDeclaration(node) || ts.isFunctionDeclaration(node))
        && node.name?.getText(tree) === name
      ) found = node
      else ts.forEachChild(node, visit)
    }
    visit(tree)
    return found
  }
  function inside(name: string, match: (node: ts.Node) => string | null): string[] {
    const texts: string[] = []
    function visit(node: ts.Node) {
      const text = match(node)
      if (text !== null) texts.push(text.replace(/\s+/g, ' '))
      ts.forEachChild(node, visit)
    }
    const root = declaration(name)
    if (root) visit(root)
    return texts
  }
  const disabledProps = (name: string) => inside(name, (node) =>
    ts.isPropertyAssignment(node) && node.name.getText(tree) === 'disabled'
      ? node.initializer.getText(tree)
      : null)
  const pickCalls = (name: string) => inside(name, (node) =>
    ts.isCallExpression(node) && node.expression.getText(tree) === 'mentionPick'
      ? node.getText(tree)
      : null)

  it('marks the state row and the entries from the pick', () => {
    expect(disabledProps('mentionEmptyOption')).toEqual(["mentionPick(undefined).kind === 'none'"])
    expect(disabledProps('mentionOptions')).toEqual(["mentionPick(row).kind === 'none'"])
  })

  it('lets click and Enter run the same pick', () => {
    expect(pickCalls('selectMentionOption')).toEqual(['mentionPick(mentionRows[index])'])
  })
})

describe('a document that is gone', () => {
  // An attached report the desk no longer knows (deleted), a pinned canvas
  // document of another session, an editing target that vanished: the
  // capsule stays, marked, and the send waits until the user removes it —
  // the server would refuse the id with 400, and dropping it silently would
  // run the task without the material the user named.
  const send = (html: string) => html.match(/<button[^>]*data-testid="agent-submit"[^>]*>/)?.[0] ?? ''
  const known = { label: 'co2', markdown: '# Bericht', runId: 'run_r1', title: 'CO2-Flottengrenzwerte' }

  it('holds the send and says why while an attached report is gone', () => {
    const html = composer({
      draftQuestion: 'Vergleiche bitte',
      engineMode: 'agent_kernel',
      onReportIdsChange: noop,
      reportCatalog: [known],
      reportIds: ['run_gone'],
      reportOptions: [known],
    })
    expect(html).toContain(translations.de.agent.composer.unavailableAttached)
    expect(send(html)).toContain('disabled=""')
  })

  it('finds a report kept out of the menus, so it is not gone', () => {
    const html = composer({
      draftQuestion: 'Vergleiche bitte',
      engineMode: 'agent_kernel',
      onReportIdsChange: noop,
      reportCatalog: [known],
      reportIds: ['run_r1'],
      reportOptions: [],
    })
    expect(html).not.toContain(translations.de.agent.composer.unavailableAttached)
    expect(send(html)).not.toContain('disabled=""')
  })

  it('holds the send for a vanished editing target and canvas document too', () => {
    expect(composer({
      documents: [],
      draftQuestion: 'Überarbeite bitte',
      selectedDocumentId: 'doc_gone',
    })).toContain(translations.de.agent.composer.unavailableAttached)
    expect(composer({
      canvasDocuments: [],
      draftQuestion: 'Kürze bitte',
      engineMode: 'agent_kernel',
      pinnedCanvasDocumentId: 'a_gone',
    })).toContain(translations.de.agent.composer.unavailableAttached)
  })
})
