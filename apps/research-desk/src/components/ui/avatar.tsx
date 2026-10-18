import { cn } from '@/lib/utils'

const graphemeSegmenter = typeof Intl.Segmenter === 'function'
  ? new Intl.Segmenter(undefined, { granularity: 'grapheme' })
  : null

/** Return one user-perceived character without splitting a UTF-16 surrogate.
 * Modern target browsers use Unicode grapheme segmentation so emoji modifiers,
 * joiners, and combining marks stay intact. The fallback remains code-point
 * safe for runtimes without Intl.Segmenter. */
function firstGrapheme(value: string): string {
  if (graphemeSegmenter) {
    for (const { segment } of graphemeSegmenter.segment(value)) return segment
  }
  return Array.from(value)[0] ?? ''
}

/** Derive up to two uppercase initials from a display name or email.
 * Exported for the unit tests; the fallback chain is name words ->
 * email local part -> "?" so the avatar never renders empty. */
export function initialsFor(displayName: string | null, email: string | null): string {
  const name = (displayName ?? '').trim()
  if (name) {
    const words = name.split(/\s+/).filter(Boolean)
    const first = firstGrapheme(words[0] ?? '')
    const second = words.length > 1 ? firstGrapheme(words[words.length - 1]) : ''
    return `${first}${second}`.toUpperCase() || '?'
  }
  const local = (email ?? '').split('@')[0]?.trim()
  if (local) return firstGrapheme(local).toUpperCase()
  return '?'
}

type InitialsAvatarProps = {
  displayName: string | null
  email: string | null
  size?: 'sm' | 'md'
  className?: string
}

/** Round initials badge for the signed-in identity.
 * Control primitive: owns its raw text sizes so feature code never uses
 * ad-hoc pixel sizes. Brand tones mark identity, one of the few uses the
 * brand accent is reserved for. */
export function InitialsAvatar({ displayName, email, size = 'md', className }: InitialsAvatarProps) {
  return (
    <span
      aria-hidden
      className={cn(
        'grid shrink-0 select-none place-items-center rounded-full bg-brand-subtle font-semibold uppercase text-brand',
        size === 'md' ? 'size-7 text-[11px]' : 'size-5 text-[10px]',
        className,
      )}
    >
      {initialsFor(displayName, email)}
    </span>
  )
}
