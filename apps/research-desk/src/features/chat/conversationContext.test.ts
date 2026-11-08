import { describe, expect, it } from 'vitest'

import type {
  ChatMessageAttachmentRecord,
  ChatMessageRecord,
} from '@/features/project/types'
import {
  chatContextEstimate,
  currentMessageContent,
  earlierMaterial,
} from './conversationContext'
import { buildChatRetryMessages } from './retry'

const report: ChatMessageAttachmentRecord = {
  attachedAt: '2026-11-06T23:08:00.000Z',
  contentMarkdown: 'BERICHT: Ausnahme Wasser gilt für Geräte in nasser Umgebung.',
  kind: 'research-report',
  runId: 'run_battery',
  title: 'EU-Batterieverordnung',
}
const rule: ChatMessageAttachmentRecord = {
  attachedAt: '2026-11-06T23:08:00.000Z',
  contentMarkdown: 'REGEL: Antworte in Stichpunkten.',
  kind: 'chat-rule',
  label: 'Stichpunkte',
  ruleId: 'rule_bullets',
  title: 'Stichpunkte',
}

const werkstatt: ChatMessageAttachmentRecord = {
  attachedAt: '2026-11-06T23:09:00.000Z',
  contentMarkdown: 'DATEI: Werkstattordnung.',
  fileId: 'fa_werkstatt',
  kind: 'file-asset',
  label: 'werkstatt',
  pageCount: null,
  sizeBytes: 10,
  title: 'Werkstattordnung',
}

function member(fileId: string, contentMarkdown: string): ChatMessageAttachmentRecord {
  return {
    attachedAt: '2026-11-06T23:10:00.000Z',
    contentMarkdown,
    fileId,
    groupId: 'fg_ordnungen',
    groupLabel: 'ordnungen',
    kind: 'file-group',
    label: fileId,
    pageCount: null,
    sizeBytes: 10,
    title: fileId,
  }
}

function message(
  id: string,
  role: 'assistant' | 'user',
  contentMarkdown: string,
  attachments?: ChatMessageAttachmentRecord[],
): ChatMessageRecord {
  return { attachments, contentMarkdown, createdAt: '2026-11-06T23:08:00.000Z', id, role }
}

// A follow-up to a question about an attached report needs the report: with
// the report only in the round of its chip, the follow-up answers that the
// exception cannot be checked.
const history = [
  message('u1', 'user', 'Welche Ausnahmen gibt es?', [report, rule]),
  message('a1', 'assistant', 'Vier Ausnahmen, darunter Wasser.'),
]

describe('material of earlier rounds stays in the conversation', () => {
  it('carries an earlier report into the follow-up, once, and leaves the rule behind', () => {
    const content = currentMessageContent('Belege die Wasser-Ausnahme.', history, [])
    expect(content).toContain('BERICHT: Ausnahme Wasser')
    expect(content.split('BERICHT:').length - 1).toBe(1)
    expect(content).not.toContain('REGEL:')
    expect(content.endsWith('Belege die Wasser-Ausnahme.')).toBe(true)
  })

  it('sends a report attached again in its current copy, as the pill the user numbered', () => {
    const fresh = { ...report, contentMarkdown: 'BERICHT NEU: aktueller Stand.' }
    const later = [...history, message('u2', 'user', 'Und die Datei?', [werkstatt]), message('a2', 'assistant', 'Ja.')]
    const content = currentMessageContent('Vergleiche [1] mit der Datei.', later, [fresh])
    expect(content).not.toContain('BERICHT: Ausnahme Wasser')
    expect(content).toContain('--- [1] @research:EU-Batterieverordnung ---\nTitle: EU-Batterieverordnung\nBERICHT NEU')
    expect(content.indexOf('--- End context 1 ---')).toBeLessThan(content.indexOf('--- @files:werkstatt ---'))
  })

  it('keeps the numbers of the current pills when earlier material comes along', () => {
    const content = currentMessageContent('Was regelt [1]?', history, [werkstatt])
    expect(content).toContain('--- [1] @files:werkstatt ---\nTitle: Werkstattordnung')
    expect(content).not.toContain('[2]')
    expect(content).toContain('--- @research:EU-Batterieverordnung ---\nTitle: EU-Batterieverordnung\nBERICHT:')
  })

  it('carries a file once, whether it came on its own or with its group', () => {
    const later = [message('u1', 'user', 'Lies die Datei.', [werkstatt]), message('a1', 'assistant', 'Ok.')]
    const content = currentMessageContent('Und die Gruppe?', later, [member('fa_werkstatt', 'DATEI: Werkstattordnung.'), member('fa_park', 'DATEI: Parkordnung.')])
    expect(content.split('DATEI: Werkstattordnung.').length - 1).toBe(1)
    expect(earlierMaterial(later, [member('fa_werkstatt', 'DATEI: Werkstattordnung.')])).toEqual([])
  })

  it('carries a group attached again with its members of that moment only', () => {
    const earlier = [
      message('u1', 'user', 'Lies die Gruppe.', [member('fa_werkstatt', 'DATEI: Werkstattordnung.'), member('fa_park', 'DATEI: Parkordnung.')]),
      message('a1', 'assistant', 'Ok.'),
      message('u2', 'user', 'Noch einmal die Gruppe.', [member('fa_werkstatt', 'DATEI: Werkstattordnung.')]),
      message('a2', 'assistant', 'Ok.'),
    ]
    expect(earlierMaterial(earlier, []).map((attachment) => attachment.contentMarkdown)).toEqual([
      'DATEI: Werkstattordnung.',
    ])
  })

  it('counts the earlier material in the composer meter', () => {
    const plain = chatContextEstimate([], [])
    const carried = chatContextEstimate(history, [])
    expect(plain.reports).toBe(0)
    expect(carried.reports).toBeGreaterThan(0)
    expect(carried.rules).toBe(0)
  })

  it('keeps the material when an answer is retried', () => {
    const followUp = message('u2', 'user', 'Belege die Wasser-Ausnahme.')
    const messages = buildChatRetryMessages({
      assistantMessage: message('a2', 'assistant', 'Nicht belegbar.'),
      history,
      userMessage: followUp,
    }, 'plain')
    expect(String(messages.at(-1)?.content)).toContain('BERICHT: Ausnahme Wasser')
  })
})
