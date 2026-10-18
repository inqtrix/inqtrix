import { describe, expect, it } from 'vitest'

import { translations } from '@/i18n/translations'
import { agentRunFailureText } from './runFailure'

const de = translations.de

describe('agentRunFailureText', () => {
  it('uses a typed model failure reason without depending on provider wording', () => {
    expect(agentRunFailureText('Provider request rejected.', de, 'model_output_limit'))
      .toBe('Die konfigurierte maximale Ausgabegröße ist für dieses Modell ungültig. Bitte die Modellkonfiguration prüfen.')
    expect(agentRunFailureText('Provider request rejected.', translations.en, 'model_output_limit'))
      .toBe('The configured maximum output size is invalid for this model. Please check the model configuration.')
    expect(agentRunFailureText('Unmapped diagnostic.', de, 'unmapped_reason')).toBe('Unmapped diagnostic.')
  })

  it('turns the machine code into a sentence that names the consequence', () => {
    // A user was shown "all_tasks_failed" — a word from inside the
    // engine, with no hint of what it meant.
    const text = agentRunFailureText('all_tasks_failed', de)
    expect(text).not.toBe('all_tasks_failed')
    expect(text).toContain('kein Ergebnis')
  })

  it('keeps the detail of a coded failure', () => {
    // The planner reports invalid plans as `code: detail`; the detail
    // names the rule that was broken and must survive.
    expect(
      agentRunFailureText('plan_invalid: genau ein synthesis-Task', de),
    ).toBe('Der Plan war nicht gültig: genau ein synthesis-Task')
  })

  it('passes an unknown failure through unchanged', () => {
    // A failure the UI cannot name is still one the user must see —
    // and the backend often sends a finished German sentence already.
    const sentence = 'Die Antwort wurde nicht veröffentlicht, weil …'
    expect(agentRunFailureText(sentence, de)).toBe(sentence)
    expect(agentRunFailureText('brandneuer_code', de)).toBe('brandneuer_code')
  })

  it('names a knowledge collection that is no longer visible', () => {
    // The run fails with the capability code when a selected collection
    // disappears from the actor's view; the code itself must not reach
    // the reader.
    expect(agentRunFailureText('knowledge.collection_not_found', de))
      .toBe('Eine ausgewählte Wissenssammlung ist für diesen Lauf nicht mehr sichtbar.')
    expect(agentRunFailureText('knowledge.collection_not_found', translations.en))
      .toBe('A selected knowledge collection is no longer visible to this run.')
  })

  it('leaves an empty failure empty', () => {
    expect(agentRunFailureText('   ', de)).toBe('')
  })

  it('is translated in both locales', () => {
    expect(agentRunFailureText('all_tasks_failed', translations.en))
      .toContain('no result')
  })
})
