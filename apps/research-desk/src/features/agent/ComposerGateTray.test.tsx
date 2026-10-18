import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import { translations } from '@/i18n/translations'
import { StructuredClarificationForm } from './ComposerGateTray'
import type { AgentClarificationRecord } from './model'

const longLabel = 'Nur Quellen aus der Sammlung zum EU AI Act, ohne Websuche und ohne Anhänge'

function round(): AgentClarificationRecord {
  return {
    answer: '',
    answeredAt: null,
    answers: {},
    clarificationId: 'clr_long',
    createdAt: 1,
    defaultAssumption: '',
    optionId: '',
    options: [],
    question: 'Welche Quellen?',
    questions: [{
      id: 'q1',
      multiSelect: false,
      options: [
        { description: '', id: 'q1_o1', label: 'Alle Quellen' },
        { description: '', id: 'q1_o2', label: longLabel },
      ],
      prompt: 'Welche Quellen?',
    }],
    status: 'pending',
  }
}

describe('answer options of a clarification', () => {
  it('shows a long option in full, wrapped, where the user picks it', () => {
    // The chosen option's wording is the user's answer: a chip held to one
    // line would hide part of it at the moment of choice, and touch devices
    // have no hover to reveal it.
    const html = renderToStaticMarkup(
      <StructuredClarificationForm
        canEdit
        clarification={round()}
        onAnswer={async () => undefined}
        t={translations.de}
      />,
    )
    const chip = html.match(/<button[^>]*>([^<]*Nur Quellen[^<]*)<\/button>/)
    expect(chip?.[1]).toBe(longLabel)
    const classes = chip?.[0].match(/class="([^"]*)"/)?.[1].split(/\s+/) ?? []
    expect(classes).toContain('whitespace-normal')
    expect(classes).not.toContain('h-6')
  })
})

describe('the default assumption of a clarification', () => {
  it('shows the model text as written, also where it reads like a replacement pattern', () => {
    // A string replacement reads `$&` and `$'` in the inserted text as
    // patterns: the assumption would show the template instead of itself.
    const assumption = "Preise in $& und $' bleiben wie im Auftrag"
    const html = renderToStaticMarkup(
      <StructuredClarificationForm
        canEdit
        clarification={{ ...round(), defaultAssumption: assumption }}
        onAnswer={async () => undefined}
        t={translations.de}
      />,
    )
    // As React escapes text: `&` and `'` become entities.
    const escaped = assumption.replaceAll('&', '&amp;').replaceAll("'", '&#x27;')
    expect(html).toContain(`Standardannahme des Laufs: ${escaped}`)
  })
})
