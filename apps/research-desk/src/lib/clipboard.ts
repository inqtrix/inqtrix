/**
 * Copy text to the clipboard on every origin the app actually runs on.
 *
 * Browsers expose `navigator.clipboard` only in secure contexts (HTTPS or
 * localhost). Self-hosted Inqtrix is routinely served over plain HTTP on a
 * LAN address, where the API is `undefined` — a handler that only calls
 * `navigator.clipboard.writeText` is dead there. This helper tries the
 * async API first and falls back to the legacy `execCommand("copy")`
 * path (hidden textarea + selection), which insecure origins still
 * support. Callers receive an honest boolean and must surface a visible
 * failure state instead of staying silent.
 */
export async function copyTextToClipboard(text: string): Promise<boolean> {
  const clipboard = navigator.clipboard
  if (clipboard?.writeText) {
    try {
      await clipboard.writeText(text)
      return true
    } catch {
      // Permission refused or transient failure — the legacy path below
      // is still worth attempting before reporting an honest failure.
    }
  }
  return legacyExecCommandCopy(text)
}

function legacyExecCommandCopy(text: string): boolean {
  const host = document.createElement('textarea')
  host.value = text
  // Keep the control out of view and out of the accessibility tree, and
  // prevent the viewport from scrolling to the injected node.
  host.setAttribute('readonly', '')
  host.setAttribute('aria-hidden', 'true')
  host.style.position = 'fixed'
  host.style.top = '0'
  host.style.left = '0'
  host.style.opacity = '0'
  host.style.pointerEvents = 'none'
  const active = document.activeElement
  document.body.appendChild(host)
  try {
    host.select()
    host.setSelectionRange(0, host.value.length)
    return document.execCommand('copy')
  } catch {
    return false
  } finally {
    host.remove()
    if (
      typeof HTMLElement !== 'undefined'
      && active instanceof HTMLElement
    ) {
      active.focus({ preventScroll: true })
    }
  }
}
