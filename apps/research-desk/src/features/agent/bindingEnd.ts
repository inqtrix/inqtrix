/**
 * A run its workflow binding ended, and the way on from it.
 *
 * The server ends a run that already ran under a workflow the deployment no
 * longer runs, or whose mode it took out; an instance admin ends one bound to a
 * workflow this server does not run. Nothing of it can continue; a new run
 * with the same question works with what runs now. Starting one goes through
 * the ordinary send, so every check a new run meets applies to it.
 */

import type { TranslationDictionary } from '@/i18n/translations'

import type { AgentRunRecord } from './model'
import { offersFullQuestion, revealedQuestion } from './questionReveal'

/** The text for a run its binding ended; `null` for every other run. */
export function bindingEndText(
  run: Pick<AgentRunRecord, 'status' | 'cancelReason'>,
  t: TranslationDictionary,
): string | null {
  if (run.status !== 'cancelled') return null
  if (run.cancelReason === 'workflow_contract_changed') {
    return t.agent.timeline.cancelledWorkflowChanged
  }
  if (run.cancelReason === 'workflow_retired') {
    return t.agent.timeline.cancelledWorkflowRetired
  }
  if (run.cancelReason === 'cancelled_by_admin') {
    return t.agent.timeline.cancelledByAdmin
  }
  return null
}

/**
 * The question a new run asks again: the full text where the run row holds
 * only its start. A failed read rejects: putting back only the start would
 * have the new run answer a cut question.
 */
export async function questionToAskAgain(
  run: Pick<AgentRunRecord, 'runId' | 'question' | 'questionClipped'>,
  readFull: (runId: string) => Promise<{ question_full?: string | null }>,
): Promise<string> {
  if (!offersFullQuestion(run)) return run.question
  return revealedQuestion(run.question, (await readFull(run.runId)).question_full)
}

/** What the last restart of a run left to say next to its button: the full
 * question could not be read, or the input holds a draft to replace or keep. */
export type AgentRestartNotice =
  | { kind: 'read_failed'; runId: string }
  | { kind: 'replace'; question: string; runId: string }

/** Whether a restart fills the input at once or first asks: a draft the user
 * typed is never replaced without a word. */
export function restartChoice(draft: string, question: string): 'fill' | 'ask' {
  const kept = draft.trim()
  return kept === '' || kept === question.trim() ? 'fill' : 'ask'
}
