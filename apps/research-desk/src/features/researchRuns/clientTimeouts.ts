import type { InqtrixCapabilities } from './types'

/**
 * Client-side abort budgets, derived from the server's published HTTP waits.
 *
 * The browser must abort an AI request AFTER the backend's own deadline so the
 * server's specific 504 (with a clear message) wins instead of a generic client
 * abort. Those server deadlines are configurable (EDITOR_ASSISTANT_TIMEOUT,
 * MAX_TOTAL_SECONDS, ...), so the client reads the effective values from
 * `/v1/capabilities` (the `timeouts` block) and adds a margin, rather than
 * hardcoding a number that would silently cap a raised server-side timeout.
 *
 * Pure functions, so they are unit-testable; the one-time "older backend"
 * notice for the fallback path is surfaced by the caller (No Silent Fallbacks),
 * see {@link isMissingServerTimeouts}.
 */

/** Grace (ms) the browser waits beyond the server's HTTP deadline. */
export const CLIENT_WAIT_MARGIN_MS = 30_000

/** Editor-run abort used only when the backend exposes no timeouts block.
 * Mirrors the shipped 600s operation + 30s server wait + client margin. */
export const EDITOR_FALLBACK_TIMEOUT_MS = 660_000

/** Chat-chain abort used only when the backend exposes no timeouts block.
 * Mirrors the shipped 3600s run + 30s server wait + client margin. */
export const CHAT_STEP_FALLBACK_TIMEOUT_MS = 3_660_000

export function deriveEditorAbortMs(capabilities: InqtrixCapabilities | null): number {
  const waitSeconds = capabilities?.timeouts?.editor_wait_seconds
  if (typeof waitSeconds === 'number' && waitSeconds > 0) {
    return waitSeconds * 1000 + CLIENT_WAIT_MARGIN_MS
  }
  return EDITOR_FALLBACK_TIMEOUT_MS
}

export function deriveChatStepTimeoutMs(capabilities: InqtrixCapabilities | null): number {
  const waitSeconds = capabilities?.timeouts?.chat_wait_seconds
  if (typeof waitSeconds === 'number' && waitSeconds > 0) {
    return waitSeconds * 1000 + CLIENT_WAIT_MARGIN_MS
  }
  return CHAT_STEP_FALLBACK_TIMEOUT_MS
}

/**
 * True when a real backend is present but advertises no `timeouts` block (an
 * older server). The caller surfaces this once so the fallback budgets above
 * are never silent. A null manifest (offline / pre-discovery / demo) is not a
 * missing-block condition and does not warrant a notice.
 */
export function isMissingServerTimeouts(capabilities: InqtrixCapabilities | null): boolean {
  return capabilities != null && capabilities.timeouts == null
}
