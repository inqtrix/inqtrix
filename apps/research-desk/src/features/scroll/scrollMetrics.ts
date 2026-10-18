/**
 * Pure scroll geometry helpers shared by the chat and knowledge message views.
 * Kept side-effect free so both surfaces (and their tests) compute "how far from
 * the bottom" and "should we follow this update" identically.
 */

/** Distance-from-bottom (px) at or below which the view still counts as "at the
 * bottom" — the user is following the conversation, so new content should stick.
 * 96px absorbs a partially-visible last line without feeling like a jump. */
export const SCROLL_AUTO_FOLLOW_THRESHOLD_PX = 96

export type ScrollMetrics = {
  clientHeight: number
  scrollHeight: number
  scrollTop: number
}

/** How the next programmatic scroll should behave: jump instantly (`auto`),
 * animate (`smooth`), or leave the position alone (`none`). */
export type ScrollFollowMode = 'auto' | 'none' | 'smooth'

export function readScrollMetrics(viewport: HTMLElement): ScrollMetrics {
  return {
    clientHeight: viewport.clientHeight,
    scrollHeight: viewport.scrollHeight,
    scrollTop: viewport.scrollTop,
  }
}

export function distanceFromBottom(metrics: ScrollMetrics): number {
  return metrics.scrollHeight - metrics.scrollTop - metrics.clientHeight
}

export function isNearBottom(
  metrics: ScrollMetrics,
  thresholdPx = SCROLL_AUTO_FOLLOW_THRESHOLD_PX,
): boolean {
  return distanceFromBottom(metrics) <= thresholdPx
}

/**
 * Decide how a same-surface content update should scroll.
 *
 * `keyChanged` (a thread/session switch) always restores instantly (`auto`) — a
 * switch is never animated. Otherwise, if the user has scrolled away from the
 * bottom we leave them alone (`none`). When they are following along we jump
 * instantly while a message is actively streaming (or reduced motion is
 * requested) and animate only for a settled append.
 */
export function scrollFollowModeForUpdate({
  hasActiveContent,
  keyChanged,
  nearBottom,
  reduceMotion,
}: {
  hasActiveContent: boolean
  keyChanged: boolean
  nearBottom: boolean
  reduceMotion: boolean | null
}): ScrollFollowMode {
  if (keyChanged) return 'auto'
  if (!nearBottom) return 'none'
  return reduceMotion || hasActiveContent ? 'auto' : 'smooth'
}
