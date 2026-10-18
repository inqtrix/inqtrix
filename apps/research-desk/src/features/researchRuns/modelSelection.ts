import type { AgentOverrides, ChatModelTier } from './types'

/**
 * The ONE picker-selection -> `agent_overrides` slice builder.
 * An explicitly picked model wins over the tier (mirrors the backend's
 * `explicit_request` resolution); an empty effort inherits the provider
 * default. Returns `undefined` when nothing is selected so callers can
 * spread it away entirely.
 */
export function modelOverridesFromSelection(
  tier: ChatModelTier | null,
  model: string | null,
  effort: string | null,
): Pick<AgentOverrides, 'model' | 'modelTier' | 'effort'> | undefined {
  if (model) return effort ? { model, effort } : { model }
  if (tier) return { modelTier: tier }
  return undefined
}

/** Build the complete Agent Desk override payload for an effective depth. */
export function agentOverridesFromSelection(
  depth: 'normal' | 'deep',
  tier: ChatModelTier | null,
  model: string | null,
  effort: string | null,
  agentTier: 'schnell' | 'gruendlich' | 'tief' | null = null,
): Pick<
  AgentOverrides,
  'agentTier' | 'depth' | 'model' | 'modelTier' | 'effort'
> {
  return {
    // A selected Stufe REPLACES depth on the wire (the server rejects a
    // contradictory pair and bridges tier -> depth itself).
    ...(agentTier ? { agentTier } : { depth }),
    ...(modelOverridesFromSelection(tier, model, effort) ?? {}),
  }
}
