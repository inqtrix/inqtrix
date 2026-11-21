import type { AgentTierCapability, AgentTierId } from '@/features/researchRuns/types'
import type { TranslationDictionary } from '@/i18n/translations'

/** The run-setup vocabulary — engine, permission, Stufe — shared by the
 * composer capsule (before a run) and the transcript recap (after it), so
 * both surfaces name the same choice with the same word. */
const AGENT_TIER_IDS: ReadonlySet<string> = new Set(['schnell', 'gruendlich', 'tief'])

export function agentTierIdOf(value: unknown): AgentTierId | null {
  return typeof value === 'string' && AGENT_TIER_IDS.has(value)
    ? (value as AgentTierId)
    : null
}

/** The Stufe the composer shows and submits: a pick on this page, otherwise
 * the user's last pick while the ladder still offers it, otherwise the
 * published default, otherwise Gründlich. Derived on every render, so a last
 * pick that arrives after the first paint (the account row, another tab) is
 * shown as long as nothing was picked on this page. Null where the server
 * publishes no ladder. */
export function effectiveAgentTier({
  defaultTier,
  last,
  selected,
  tiers,
}: {
  defaultTier: AgentTierId | undefined
  last: string
  selected: AgentTierId | null
  tiers: readonly AgentTierCapability[] | null
}): AgentTierId | null {
  if (!tiers) return null
  if (selected) return selected
  return tiers.find((tier) => tier.id === last)?.id ?? defaultTier ?? 'gruendlich'
}

/** The engine's name in the run setup: the kernel picks its own way. */
export function engineModeLabel(t: TranslationDictionary): string {
  return t.agent.composer.engineAuto
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
 * here because it decides the run's cost class (one search, research
 * children, a review pass); a Stufe the user cannot see before sending is a
 * Stufe they cannot correct.
 *
 * The response form belongs here for the same reason, and it closes a
 * measured gap: picking "Canvas" changed the capsule by nothing at all, so
 * an armed session state was legible only inside the opened menu. `auto`
 * stays silent — it is the default and says nothing the engine label does
 * not already say. */
export function runSetupSummary(
  selection: {
    autonomy: string
    responseForm?: 'auto' | 'chat' | 'canvas'
    tierMode: AgentTierId | null
  },
  t: TranslationDictionary,
): string {
  const parts = [
    engineModeLabel(t),
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
