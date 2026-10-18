/**
 * Pure presentation helpers for model cards. The single home for cost-tier,
 * token-format, reasoning-preset and capability-label logic so feature code
 * never re-derives them; the model-picker components render the results.
 */
import type { ModelCard } from '@/features/researchRuns/types'

/** A selectable reasoning level; `token` is the wire effort value. */
export type ReasoningLevelOption = { token: string; label: string }

/** Compact display labels for the known effort tokens. Kept short so they fit a
 * segmented control cell and read cleanly as a trigger suffix. */
const EFFORT_LABELS: Record<string, string> = {
  none: 'Off',
  minimal: 'Min',
  low: 'Low',
  medium: 'Med',
  high: 'High',
  xhigh: 'XHigh',
  max: 'Max',
}

/** Short display label for one reasoning-effort token (falls back to a
 * capitalised form of the raw token for any future level). */
export function effortLevelLabel(token: string): string {
  return EFFORT_LABELS[token] ?? token.charAt(0).toUpperCase() + token.slice(1)
}

/**
 * Map a model's accepted effort tokens to one selectable option per level, in
 * the model's own (increasing-depth) order — every level the model supports is
 * selectable, model-dependent (no collapsing to fixed buckets). An empty input
 * means the model has no effort control, so the picker hides the selector.
 */
export function reasoningLevelOptions(levels: string[]): ReasoningLevelOption[] {
  return levels.map((token) => ({ token, label: effortLevelLabel(token) }))
}

/** Coarse `$`..`$$$$` cost tier (by output price) plus the exact value string. */
export function costTier(pricing: ModelCard['pricing']): { signs: string; value: string } {
  const output = pricing.output_per_mtok
  const signs = output <= 5 ? '$' : output <= 15 ? '$$' : output <= 30 ? '$$$' : '$$$$'
  return { signs, value: `$${output}/1M` }
}

/** Compact token count: exact below 1k, "1.2k" in thousands, "1.05M" in
 * millions. Used for both the small live meter number and the KONTEXT tile. */
export function formatTokens(count: number): string {
  if (count >= 1_000_000) {
    const millions = count / 1_000_000
    return `${Number.isInteger(millions) ? millions : Number(millions.toFixed(2))}M`
  }
  if (count >= 1000) {
    const thousands = count / 1000
    return `${Number.isInteger(thousands) ? thousands : Number(thousands.toFixed(1))}k`
  }
  return `${count}`
}

/** Compact byte count for the quota meter: "0", "512 B", "4.0 KB",
 * "45 MB", "1.2 GB" (1024-based, one decimal above KB). */
export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  const units = ['KB', 'MB', 'GB', 'TB']
  let value = bytes / 1024
  let unit = 0
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024
    unit += 1
  }
  return `${Number.isInteger(value) ? value : Number(value.toFixed(1))} ${units[unit]}`
}

const CAPABILITY_LABELS: Record<string, string> = {
  reasoning: 'Reasoning',
  code: 'Code',
  tool_use: 'Tool-Use',
  vision: 'Vision',
}

/** Display label for a capability tag (falls back to the raw tag). */
export function capabilityLabel(capability: string): string {
  return CAPABILITY_LABELS[capability] ?? capability
}

/** Capitalised TEMPO label for the hover-card (e.g. "mittel" -> "Mittel"). */
export function speedLabel(speed: string): string {
  return speed.charAt(0).toUpperCase() + speed.slice(1)
}

/** The reader-facing description of one model, in the UI language.
 *
 * The server ships a German-only `card.description` in `models_catalog`
 * (`/health` is unauthenticated and knows no user locale), so the picker used
 * to show an English menu with German model lines. Product copy lives with
 * product copy: the translated text wins, and the server's own string is the
 * fallback for a model the table does not know yet — a new deployment model
 * still reads as something rather than nothing.
 */
export function modelCardDescription(
  card: { id: string; description: string },
  descriptions: Record<string, string>,
): string {
  return descriptions[card.id] ?? card.description
}
