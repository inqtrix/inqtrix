import { describe, expect, it } from 'vitest'
import {
  buildMentionOptions,
  detectMentionTrigger,
  type MentionCategoryLabels,
  type MentionSources,
} from './mention'

const sources: MentionSources = {
  fileGroupOptions: [{ fileCount: 2, groupId: 'g1', label: 'dossier', title: 'Dossier' }],
  fileOptions: [{ fileId: 'f1', label: 'alpha', pageCount: 3, sizeBytes: 10, title: 'alpha.txt' }],
  reportOptions: [],
  ruleOptions: [],
}

const labels: MentionCategoryLabels = {
  files: 'Files',
  filegroups: 'File groups',
  research: 'Research',
  rules: 'Rules',
}

describe('detectMentionTrigger', () => {
  it('matches a bare @ as the root category step', () => {
    const match = detectMentionTrigger('@', 1)
    expect(match?.kind).toBe('root')
  })

  it('matches a typed @files: token with its query', () => {
    const match = detectMentionTrigger('use @files:al', 'use @files:al'.length)
    expect(match).toMatchObject({ kind: 'files', query: 'al' })
  })

  it('matches @filegroups: tokens', () => {
    const match = detectMentionTrigger('@filegroups:', '@filegroups:'.length)
    expect(match?.kind).toBe('filegroups')
  })

  it('returns null when there is no trigger', () => {
    expect(detectMentionTrigger('plain text', 10)).toBeNull()
  })
})

describe('buildMentionOptions', () => {
  it('lists all enabled categories at the root step', () => {
    const match = detectMentionTrigger('@', 1)!
    const options = buildMentionOptions(match, sources, labels, ['research', 'rules', 'files', 'filegroups'])
    expect(options.map((option) => option.type)).toEqual(['research', 'rules', 'files', 'filegroups'])
  })

  it('lists file items filtered by the query', () => {
    const match = detectMentionTrigger('@files:al', '@files:al'.length)!
    const options = buildMentionOptions(match, sources, labels, ['rules', 'files', 'filegroups'])
    expect(options).toHaveLength(1)
    expect(options[0].ref).toEqual({ fileId: 'f1', kind: 'file-asset' })
  })

  it('lists file groups with their member count title', () => {
    const match = detectMentionTrigger('@filegroups:', '@filegroups:'.length)!
    const options = buildMentionOptions(match, sources, labels, ['rules', 'files', 'filegroups'])
    expect(options[0].ref).toEqual({ groupId: 'g1', kind: 'file-group' })
  })

  it('carries prompt categories for grouped @rules: autocomplete rendering', () => {
    const match = detectMentionTrigger('@rules:', '@rules:'.length)!
    const options = buildMentionOptions(
      match,
      {
        ...sources,
        ruleOptions: [
          {
            category: 'instruction',
            includeInAutocomplete: true,
            label: 'style',
            linkedContextRefs: [],
            markdown: 'Write concise answers.',
            ruleId: 'r1',
            title: 'Style',
            visibility: { agent: false, chat: true, editor: true },
          },
          {
            category: 'function',
            includeInAutocomplete: true,
            label: 'translate',
            linkedContextRefs: [],
            markdown: 'Translate the input.',
            ruleId: 'r2',
            title: 'Translate',
            visibility: { agent: false, chat: true, editor: true },
          },
        ],
      },
      labels,
      ['rules', 'files', 'filegroups'],
    )

    expect(options.map((option) => option.category)).toEqual(['instruction', 'function'])
    expect(options.map((option) => option.ref)).toEqual([
      { kind: 'chat-rule', ruleId: 'r1' },
      { kind: 'chat-rule', ruleId: 'r2' },
    ])
  })

  it('sorts rule items into instruction -> function -> context order', () => {
    const match = detectMentionTrigger('@rules:', '@rules:'.length)!
    const options = buildMentionOptions(
      match,
      {
        ...sources,
        ruleOptions: [
          {
            category: 'context',
            includeInAutocomplete: true,
            label: 'pack',
            linkedContextRefs: [],
            markdown: 'Context pack.',
            ruleId: 'r-context',
            title: 'Pack',
            visibility: { agent: false, chat: true, editor: true },
          },
          {
            category: 'function',
            includeInAutocomplete: true,
            label: 'translate',
            linkedContextRefs: [],
            markdown: 'Translate the input.',
            ruleId: 'r-function',
            title: 'Translate',
            visibility: { agent: false, chat: true, editor: true },
          },
          {
            category: 'instruction',
            includeInAutocomplete: true,
            label: 'style',
            linkedContextRefs: [],
            markdown: 'Write concise answers.',
            ruleId: 'r-instruction',
            title: 'Style',
            visibility: { agent: false, chat: true, editor: true },
          },
        ],
      },
      labels,
      ['rules', 'files', 'filegroups'],
    )

    expect(options.map((option) => option.category)).toEqual(['instruction', 'function', 'context'])
  })
})
