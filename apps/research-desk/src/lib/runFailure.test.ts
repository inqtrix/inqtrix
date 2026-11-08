import { describe, expect, it } from 'vitest'

import { translations } from '@/i18n/translations'
import { runFailureText } from './runFailure'

const de = translations.de

describe('runFailureText', () => {
  it('uses a typed model failure reason without depending on provider wording', () => {
    expect(runFailureText('Provider request rejected.', de, 'model_output_limit'))
      .toBe('Die konfigurierte maximale Ausgabegröße ist für dieses Modell ungültig. Bitte die Modellkonfiguration prüfen.')
    expect(runFailureText('Provider request rejected.', translations.en, 'model_output_limit'))
      .toBe('The configured maximum output size is invalid for this model. Please check the model configuration.')
    expect(runFailureText('Unmapped diagnostic.', de, 'unmapped_reason')).toBe('Unmapped diagnostic.')
  })

  it('turns the machine code into a sentence that names the consequence', () => {
    // A user was shown "all_tasks_failed" — a word from inside the
    // engine, with no hint of what it meant.
    const text = runFailureText('all_tasks_failed', de)
    expect(text).not.toBe('all_tasks_failed')
    expect(text).toContain('kein Ergebnis')
  })

  it('keeps the detail of a coded failure', () => {
    // The planner reports invalid plans as `code: detail`; the detail
    // names the rule that was broken and must survive.
    expect(
      runFailureText('plan_invalid: genau ein synthesis-Task', de),
    ).toBe('Der Plan war nicht gültig: genau ein synthesis-Task')
  })

  it('passes an unknown failure through unchanged', () => {
    // A failure the UI cannot name is still one the user must see —
    // and the backend often sends a finished German sentence already.
    const sentence = 'Die Antwort wurde nicht veröffentlicht, weil …'
    expect(runFailureText(sentence, de)).toBe(sentence)
    expect(runFailureText('brandneuer_code', de)).toBe('brandneuer_code')
  })

  it('names a knowledge collection that is no longer visible', () => {
    // The run fails with the capability code when a selected collection
    // disappears from the actor's view; the code itself must not reach
    // the reader.
    expect(runFailureText('knowledge.collection_not_found', de))
      .toBe('Eine ausgewählte Wissenssammlung ist für diesen Lauf nicht mehr sichtbar.')
    expect(runFailureText('knowledge.collection_not_found', translations.en))
      .toBe('A selected knowledge collection is no longer visible to this run.')
  })

  it('names a run stopped by a withdrawn access without promising why', () => {
    // One code covers a blocked account, a changed skill and a withdrawn
    // collection, so the sentence stays general. Spelled out on purpose: a
    // reword must be caught here. The server's English detail must not show.
    const detail = 'access to a pinned knowledge collection was revoked'
    expect(runFailureText(detail, de, 'authorization_revoked'))
      .toBe('Der Auftrag wurde gestoppt, weil ein benötigter Zugriff nicht mehr erlaubt ist.')
    expect(runFailureText(detail, translations.en, 'authorization_revoked'))
      .toBe('The task was stopped because access it needs is no longer permitted.')
  })

  it('leaves an empty failure empty', () => {
    expect(runFailureText('   ', de)).toBe('')
  })

  it('is translated in both locales', () => {
    expect(runFailureText('all_tasks_failed', translations.en))
      .toContain('no result')
  })
})
