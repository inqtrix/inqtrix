import { readFileSync } from 'node:fs'

import { describe, expect, it } from 'vitest'
import {
  composerCommandEffect,
  extractTrailingComposerCommand,
  knowledgePolicyForScope,
  knowledgeScopeForSubmit,
  knowledgeSwitchState,
  normalizeAgentKnowledgeSwitch,
  webSwitchState,
  normalizeAgentSourcePolicy,
  normalizeAgentExecutionSnapshot,
  resolveAgentExecutionDisplay,
  toolUseCountsFromSnapshot,
} from './executionPolicy'
import { COMPOSER_COMMAND_OPTIONS } from './skillSlash'

describe('extractTrailingComposerCommand', () => {
  it('reads an exact trailing command and strips it from the question', () => {
    expect(extractTrailingComposerCommand('Wer gewann heute? /web')).toEqual({
      command: 'web',
      question: 'Wer gewann heute?',
    })
    expect(extractTrailingComposerCommand('Fasse den Bestand zusammen /WISSEN  ')).toEqual({
      command: 'wissen',
      question: 'Fasse den Bestand zusammen',
    })
    expect(extractTrailingComposerCommand('Vergleiche die Optionen /canvas')).toEqual({
      command: 'canvas',
      question: 'Vergleiche die Optionen',
    })
  })

  it('does not reinterpret paths, inline tokens, or unknown commands', () => {
    expect(extractTrailingComposerCommand('https://example.com/web')).toBeNull()
    expect(extractTrailingComposerCommand('Nutze /web und antworte')).toBeNull()
    expect(extractTrailingComposerCommand('Frage /recherche')).toBeNull()
    // `/chat` is deliberately not a command: it would only serve to escape a
    // canvas raised without being asked, and the run-setup menu still offers
    // the chat form explicitly.
    expect(extractTrailingComposerCommand('Antworte hier /chat')).toBeNull()
  })
})

describe('knowledge switch', () => {
  it('pins chips, asks for everything on an ON switch, and nothing otherwise', () => {
    expect(knowledgeScopeForSubmit({ collectionIds: ['kc_1'], knowledgeSwitch: 'off' })).toBe('pinned')
    expect(knowledgeScopeForSubmit({ collectionIds: [], knowledgeSwitch: 'all' })).toBe('all')
    expect(knowledgeScopeForSubmit({ collectionIds: [], knowledgeSwitch: 'off' })).toBe('off')
    expect(knowledgeScopeForSubmit({ collectionIds: [], knowledgeSwitch: undefined })).toBe('off')
    expect(knowledgePolicyForScope('all')).toBe('available')
    expect(knowledgePolicyForScope('pinned')).toBe('available')
    expect(knowledgePolicyForScope('off')).toBe('disabled')
  })

  it('gives every command one session effect and never a collection list', () => {
    expect(composerCommandEffect('web')).toEqual({ web: 'available' })
    expect(composerCommandEffect('wissen')).toEqual({ knowledgeSwitch: 'all' })
    expect(composerCommandEffect('canvas')).toEqual({ responseForm: 'canvas' })
    expect(Object.keys(composerCommandEffect('wissen'))).not.toContain('collectionIds')
    // Each command touches exactly ONE axis — a command that changed two
    // pieces of session state would be unpredictable from its name.
    for (const command of ['web', 'wissen', 'canvas'] as const) {
      expect(Object.keys(composerCommandEffect(command))).toHaveLength(1)
    }
  })

  it('shows the knowledge glyph only once the session asked, never beside a chip', () => {
    const base = { available: true, pinned: false }
    expect(knowledgeSwitchState({ ...base, knowledgeSwitch: undefined })).toBe('hidden')
    expect(knowledgeSwitchState({ ...base, knowledgeSwitch: 'all' })).toBe('on')
    expect(knowledgeSwitchState({ ...base, knowledgeSwitch: 'off' })).toBe('off')
    expect(knowledgeSwitchState({ ...base, knowledgeSwitch: 'all', pinned: true })).toBe('hidden')
    expect(knowledgeSwitchState({ ...base, knowledgeSwitch: 'all', available: false })).toBe('unavailable')
    expect(normalizeAgentKnowledgeSwitch('all')).toBe('all')
    expect(normalizeAgentKnowledgeSwitch('everything')).toBeUndefined()
  })
})

describe('normalizeAgentSourcePolicy', () => {
  it('defaults absent and malformed metadata to both sources available', () => {
    expect(normalizeAgentSourcePolicy(null)).toEqual({
      web: 'available',
      knowledge: 'available',
    })
    expect(normalizeAgentSourcePolicy({ web: 'mystery', knowledge: 1 })).toEqual({
      web: 'available',
      knowledge: 'available',
    })
  })

  it('preserves explicit disabled states independently', () => {
    expect(normalizeAgentSourcePolicy({ web: 'disabled' })).toEqual({
      web: 'disabled',
      knowledge: 'available',
    })
  })
})

describe('normalizeAgentExecutionSnapshot', () => {
  it('normalizes the canonical effective execution contract', () => {
    expect(normalizeAgentExecutionSnapshot({
      execution: {
        effective_mode: 'agent_kernel',
        response_form: 'chat',
        depth: 'normal',
        model: 'model-x',
        reasoning_effort: 'high',
        source_policy: { web: 'available', knowledge: 'disabled' },
        knowledge_scope: 'all',
        knowledge_collection_count: 3,
        consent_reason: 'explicit_directive',
        tool_use_counts: { web: 1, knowledge: 0 },
        web_searches: 1,
        limits: {
          tool_calls: {
            used: 12,
            limit: 30,
            ceiling: 60,
            recoverable: true,
            extendable: true,
          },
          tokens: {
            used: 900,
            limit: 1000,
            ceiling: 1000,
            recoverable: false,
            extendable: false,
            reason: 'operator_ceiling_exactly_once_required',
          },
        },
      },
    })).toEqual({
      effectiveMode: 'agent_kernel',
      responseForm: 'chat',
      depth: 'normal',
      model: 'model-x',
      reasoningEffort: 'high',
      sourcePolicy: { web: 'available', knowledge: 'disabled' },
      knowledgeScope: 'all',
      knowledgeCollectionCount: 3,
      consentReason: 'explicit_directive',
      toolUseCounts: { web: 1, knowledge: 0 },
      webSearches: 1,
      limits: {
        tool_calls: {
          used: 12,
          limit: 30,
          ceiling: 60,
          recoverable: true,
          extendable: true,
          reason: null,
        },
        tokens: {
          used: 900,
          limit: 1000,
          ceiling: 1000,
          recoverable: false,
          extendable: false,
          reason: 'operator_ceiling_exactly_once_required',
        },
      },
      toolGrants: [],
    })
  })

  it('reports no knowledge scope for older servers and unknown labels', () => {
    expect(normalizeAgentExecutionSnapshot({ execution: {} })).toMatchObject({
      knowledgeScope: null,
      knowledgeCollectionCount: 0,
    })
    expect(normalizeAgentExecutionSnapshot({
      execution: { knowledge_scope: 'everything', knowledge_collection_count: -2 },
    })).toMatchObject({ knowledgeScope: null, knowledgeCollectionCount: 0 })
    expect(normalizeAgentExecutionSnapshot({
      execution: { knowledge_scope: 'pinned', knowledge_collection_count: 2 },
    })).toMatchObject({ knowledgeScope: 'pinned', knowledgeCollectionCount: 2 })
  })

  it('drops malformed limits instead of inventing operator policy', () => {
    const normalized = normalizeAgentExecutionSnapshot({
      execution: {
        limits: {
          broken: { limit: '30' },
          fixed: { used: -1, limit: 8, ceiling: 4, extendable: true },
        },
      },
    })
    expect(normalized?.limits).toEqual({})
  })
})

describe('toolUseCountsFromSnapshot', () => {
  it('reads only finite non-negative source counts', () => {
    expect(toolUseCountsFromSnapshot({
      execution: { tool_use_counts: { web: 1, knowledge: 2 } },
    })).toEqual({ web: 1, knowledge: 2 })
    expect(toolUseCountsFromSnapshot({})).toBeNull()
  })
})

describe('resolveAgentExecutionDisplay', () => {
  it('shows the composer selection until a run was admitted', () => {
    expect(resolveAgentExecutionDisplay({
      execution: null,
      selectedMode: 'workspace_agent',
      selectedResponseForm: 'canvas',
      selectedDepth: 'deep',
    })).toEqual({
      effectiveMode: 'workspace_agent',
      responseForm: 'canvas',
      depth: 'deep',
    })
  })

  it('uses the accepted canonical snapshot once a run was admitted', () => {
    const execution = normalizeAgentExecutionSnapshot({
      execution: {
        effective_mode: 'agent_kernel',
        response_form: 'chat',
        depth: 'normal',
        source_policy: { web: 'available', knowledge: 'disabled' },
        tool_use_counts: { web: 1, knowledge: 0 },
      },
    })
    expect(resolveAgentExecutionDisplay({
      execution,
      selectedMode: 'workspace_agent',
      selectedResponseForm: 'canvas',
      selectedDepth: 'deep',
    })).toEqual({
      effectiveMode: 'agent_kernel',
      responseForm: 'chat',
      depth: 'normal',
    })
  })
})

describe('toolGrants normalization', () => {
  it('reads run-wide grants and keeps only non-empty strings', () => {
    const snapshot = normalizeAgentExecutionSnapshot({
      execution: {
        effective_mode: 'agent_kernel',
        response_form: 'chat',
        depth: 'normal',
        model: '',
        reasoning_effort: '',
        source_policy: { web: 'available', knowledge: 'available' },
        consent_reason: '',
        tool_use_counts: { web: 0, knowledge: 0 },
        limits: {},
        tool_grants: ['web_instant', '', 7, 'load_skill'],
      },
    })
    expect(snapshot?.toolGrants).toEqual(['web_instant', 'load_skill'])
  })

  it('defaults to no grants on older servers without the key', () => {
    const snapshot = normalizeAgentExecutionSnapshot({
      execution: {
        effective_mode: 'agent_kernel',
        response_form: 'chat',
        depth: 'normal',
        model: '',
        reasoning_effort: '',
        source_policy: { web: 'available', knowledge: 'available' },
        consent_reason: '',
        tool_use_counts: { web: 0, knowledge: 0 },
        limits: {},
      },
    })
    expect(snapshot?.toolGrants).toEqual([])
  })
})

describe('webSwitchState', () => {
  it('has exactly two visible states plus unavailable — on and off never depend on a hover text', () => {
    expect(webSwitchState({ available: true, policy: 'available' })).toBe('on')
    expect(webSwitchState({ available: true, policy: 'disabled' })).toBe('off')
    expect(webSwitchState({ available: false, policy: 'available' })).toBe('unavailable')
  })
})

describe('the /canvas command', () => {
  // The wire field (`response_form`) and its override carry the response
  // form on their own; the command only arms them without opening a submenu.
  // It therefore sets the EXISTING form and introduces no second output
  // switch.
  it('is one of the composer commands, and /chat is not', () => {
    const tokens = COMPOSER_COMMAND_OPTIONS.map((command) => command.token)
    expect(tokens).toContain('canvas')
    expect(tokens).not.toContain('chat')
  })

  it('reaches the same effect through the palette and a trailing token', () => {
    const trailing = extractTrailingComposerCommand('Fasse das zusammen /canvas')
    expect(trailing).toEqual({ command: 'canvas', question: 'Fasse das zusammen' })
    expect(composerCommandEffect(trailing!.command))
      .toEqual(composerCommandEffect('canvas'))
  })
})

describe('the switch capsules live in the text, derived not remembered', () => {
  // The capsules must sit INLINE, in the text flow where the command was
  // typed — a `<textarea>` cannot hold elements, so the composer uses the
  // same TipTap mechanism the chat composer already has.
  //
  // Switching the response form back by hand in the submenu therefore has to
  // drop the capsule again. That holds structurally because there is exactly
  // ONE round trip: the document reports what the user left in it, and the
  // switches write back what changed elsewhere.
  const composer = readFileSync(
    'src/features/agent/AgentComposer.tsx',
    'utf8',
  )
  const input = readFileSync(
    'src/features/composer/AgentInlineInput.tsx',
    'utf8',
  )

  it('puts the pill where the command was typed, not beside the text', () => {
    expect(composer).toContain('inputRef.current?.replaceTokenWithPill(')
    // No plain textarea — that is the only surface that would force the
    // capsules next to the prose.
    expect(composer).not.toContain('<Textarea')
    expect(composer).toContain('<AgentInlineInput')
  })

  it('derives the wanted pills from the switches', () => {
    expect(composer).toContain("knowledgeSwitch === 'all'")
    expect(composer).toContain("responseForm !== 'auto'")
    expect(composer).toContain('const commandPills: CommandPillSpec[]')
  })

  it('turns a removed pill into a switched-off state', () => {
    expect(composer).toContain('function handlePillsEdited')
    expect(composer).toContain("onKnowledgeSwitchChange?.(hasKnowledge ? 'all' : 'off')")
    expect(composer).toContain('if (nextForm !== responseForm) onResponseFormChange?.(nextForm)')
  })

  it('cannot loop between the two directions', () => {
    // The ref of the last reported set is what tells a user edit apart from
    // an external change; without it each would retrigger the other.
    expect(input).toContain('reportedRef')
    expect(input).toContain('if (sameSet(pills, reportedRef.current)) return')
  })

  it('sends the sentence the user wrote, capsules spelled out', () => {
    // A capsule holds a document position but no character, so reading the
    // bare text would send the sentence with a hole where the command stood,
    // visible in the user's own message bubble. The SENT text therefore
    // spells the label (the word they typed); the EFFECT still travels in the
    // request fields, never in the sentence.
    // The projection itself is pinned behaviourally in AgentInlineInput.test.
    expect(composer).toContain('inputRef.current?.textWithPillLabels(keptByAnswer, quote)')
    expect(composer).toContain('extractTrailingComposerCommand(written)')
    expect(composer).toContain('trailing?.question ?? written.trim()')
  })

  it('keeps /wissen one capsule instead of a chip per collection', () => {
    expect(composerCommandEffect('wissen')).toEqual({ knowledgeSwitch: 'all' })
    const pick = composer.slice(
      composer.indexOf('function selectSlashOption'),
      composer.indexOf('function handleKeyDown'),
    )
    expect(pick).not.toContain('onSelectedCollectionIdsChange')
  })
})
