import type { TranslationDictionary } from '@/i18n/translations'
import type { ChatModelTier, NodeModelResolution } from './types'

type ChatCopy = TranslationDictionary['chat']

/** What the catalog picker is actually going to send.
 *
 * The catalog branch only ever knew about explicit model ids, so a tier
 * arriving from the account preference looked identical to "nothing selected"
 * — the picker said server default while the request carried the tier. Naming
 * the three states keeps that difference visible.
 */
export function catalogSelectionKind(
  selectedModel: string | null | undefined,
  selectedTier: string | null | undefined,
): 'model' | 'tier' | 'server-default' {
  if (selectedModel != null) return 'model'
  if (selectedTier != null && selectedTier !== '') return 'tier'
  return 'server-default'
}

export function modelNameLabel(
  option: Pick<NodeModelResolution, 'model'> | null | undefined,
  fallback: string,
) {
  const model = option?.model?.trim()
  if (!model) return fallback
  return model.replace(/^.+\//, '')
}

export function modelDetailLabel(
  option: NodeModelResolution | null,
  copy: ChatCopy,
) {
  return `${modelNameLabel(option, copy.modelUnknown)} · ${modelEffortLabel(option, copy)}`
}

export function modelEffortLabel(
  option: Pick<NodeModelResolution, 'effort'> | null | undefined,
  copy: ChatCopy,
) {
  return modelEffortLabelFromToken(option?.effort, copy)
}

export function modelEffortLabelFromToken(
  effortToken: string | undefined,
  copy: ChatCopy,
) {
  const effort = effortToken?.trim().toLowerCase()
  if (!effort) return copy.modelEffortDefault
  if (effort === 'none') return copy.modelThinkingOff
  return `${copy.modelThinkingOn} ${shortEffort(effort)}`
}

export function modelTierLabel(tier: ChatModelTier, copy: ChatCopy) {
  if (tier === 'high') return copy.modelTierHigh
  if (tier === 'fast') return copy.modelTierFast
  return copy.modelTierMid
}

export function modelTierDescription(tier: ChatModelTier, copy: ChatCopy) {
  if (tier === 'high') return copy.modelTierHighDescription
  if (tier === 'fast') return copy.modelTierFastDescription
  return copy.modelTierMidDescription
}

function shortEffort(effort: string) {
  if (effort === 'medium') return 'med'
  if (effort === 'minimal') return 'min'
  return effort
}
