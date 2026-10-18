/**
 * Semantic tone tokens shared by every surface that color-codes the four
 * mention kinds / prompt categories (mention menu, prompt library). A tone maps
 * to the project design tokens `brand` / `success` / `file` / `warning`, so the
 * same hue tracks the active theme preset (`slate` / `graphite` / `sage`) and
 * dark mode without per-surface palette literals.
 */
export type MentionTone = 'brand' | 'success' | 'file' | 'warning'

/**
 * Token to Tailwind class maps. Every value is a complete literal on purpose:
 * Tailwind's JIT only keeps classes it can see as whole strings, and the repo
 * has no safelist for tone tokens, so dynamic `bg-${tone}` construction would be
 * purged. Add a new shape here rather than building class names at call sites.
 */
export const toneText: Record<MentionTone, string> = {
  brand: 'text-brand',
  success: 'text-success',
  file: 'text-file',
  warning: 'text-warning',
}

/** Solid fill for dots and the active mention bar. */
export const toneBar: Record<MentionTone, string> = {
  brand: 'bg-brand',
  success: 'bg-success',
  file: 'bg-file',
  warning: 'bg-warning',
}

/** Subtle fill + matching text for inline scope chips. */
export const toneChip: Record<MentionTone, string> = {
  brand: 'bg-brand-subtle/70 text-brand',
  success: 'bg-success-subtle/70 text-success',
  file: 'bg-file-subtle/70 text-file',
  warning: 'bg-warning-subtle/70 text-warning',
}

/** 2px left accent for the selected list row. */
export const toneAccentBorderLeft: Record<MentionTone, string> = {
  brand: 'border-l-brand',
  success: 'border-l-success',
  file: 'border-l-file',
  warning: 'border-l-warning',
}

/** Bordered subtle surface for an active selectable card. */
export const toneActiveCard: Record<MentionTone, string> = {
  brand: 'border-brand/40 bg-brand-subtle/40',
  success: 'border-success/40 bg-success-subtle/40',
  file: 'border-file/40 bg-file-subtle/40',
  warning: 'border-warning/40 bg-warning-subtle/40',
}

/** Outlined subtle badge (category tag). */
export const toneBadge: Record<MentionTone, string> = {
  brand: 'border-brand/25 bg-brand-subtle text-brand',
  success: 'border-success/25 bg-success-subtle text-success',
  file: 'border-file/25 bg-file-subtle text-file',
  warning: 'border-warning/25 bg-warning-subtle text-warning',
}

/** Filled icon tile for the usage callout. */
export const toneIconTile: Record<MentionTone, string> = {
  brand: 'bg-brand-subtle text-brand',
  success: 'bg-success-subtle text-success',
  file: 'bg-file-subtle text-file',
  warning: 'bg-warning-subtle text-warning',
}
