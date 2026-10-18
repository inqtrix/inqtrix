import type { AgentTierId } from '@/features/researchRuns/types'
import type { TranslationDictionary } from '@/i18n/translations'

import type { AgentEngineMode } from './agentStatusOverview'

/** The run-setup vocabulary — engine, permission, Stufe — shared by the
 * composer capsule (before a run) and the transcript recap (after it), so
 * both surfaces name the same choice with the same word. */
const AGENT_TIER_IDS: ReadonlySet<string> = new Set(['schnell', 'gruendlich', 'tief'])

export function agentTierIdOf(value: unknown): AgentTierId | null {
  return typeof value === 'string' && AGENT_TIER_IDS.has(value)
    ? (value as AgentTierId)
    : null
}

export function engineModeLabel(
  mode: AgentEngineMode,
  t: TranslationDictionary,
): string {
  return mode === 'agent_kernel'
    ? t.agent.composer.engineAuto
    : t.agent.overview.brainMission
}

export function autonomyModeLabel(
  mode: string,
  t: TranslationDictionary,
): string {
  if (mode === 'strict') return t.agent.composer.autonomyStrict
  if (mode === 'autonomous') return t.agent.composer.autonomyAutonomous
  return t.agent.composer.autonomyBalanced
}

/** Stufe display — one label per tier id. */
export function tierLabel(
  tier: AgentTierId,
  t: TranslationDictionary,
): string {
  if (tier === 'schnell') return t.agent.composer.tierSchnell
  if (tier === 'tief') return t.agent.composer.tierTief
  return t.agent.composer.tierGruendlich
}

/** The run-setup capsule text — what ONE turn will do and how much it
 * may do: engine · permission · Stufe · response form. The Stufe belongs
 * here because it decides the run's cost class (one search, a research
 * child, a mission); a Stufe the user cannot see before sending is a Stufe
 * they cannot correct.
 *
 * The response form belongs here for the same reason, and it closes a
 * measured gap: picking "Canvas" changed the capsule by nothing at all, so
 * an armed session state was legible only inside the opened menu. `auto`
 * stays silent — it is the default and says nothing the engine label does
 * not already say. */
export function runSetupSummary(
  selection: {
    autonomy: string
    engineMode: AgentEngineMode
    responseForm?: 'auto' | 'chat' | 'canvas'
    tierMode: AgentTierId | null
  },
  t: TranslationDictionary,
): string {
  const parts = [
    engineModeLabel(selection.engineMode, t),
    autonomyModeLabel(selection.autonomy, t),
  ]
  if (selection.tierMode) parts.push(tierLabel(selection.tierMode, t))
  if (selection.responseForm && selection.responseForm !== 'auto') {
    parts.push(responseFormLabel(selection.responseForm, t))
  }
  return parts.join(' \u00b7 ')
}

/** Response-form display — one label per form, shared by the capsule and the
 * run-setup menu so both name the same choice with the same word. */
export function responseFormLabel(
  form: 'auto' | 'chat' | 'canvas',
  t: TranslationDictionary,
): string {
  if (form === 'chat') return t.agent.composer.responseFormChat
  if (form === 'canvas') return t.agent.composer.responseFormCanvas
  return t.agent.composer.responseFormAuto
}
