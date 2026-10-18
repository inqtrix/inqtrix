/**
 * Small DOM helpers shared by the editor shell (`EditorWorkspace`) and the
 * markdown surface (`core/MarkdownEditorSurface`).
 */

export function escapeCssIdentifier(value: string) {
  if (globalThis.CSS?.escape) return globalThis.CSS.escape(value)
  return value.replace(/["\\]/g, '\\$&')
}

/** Make Tiptap's generated overflow wrappers reachable and identifiable by
 * keyboard and assistive-technology users. Tiptap owns these wrapper nodes,
 * so the surface reapplies the contract whenever the document DOM changes. */
export function applyScrollableTableSemantics(root: ParentNode, label: string) {
  const wrappers = root.querySelectorAll<HTMLElement>('.tableWrapper')
  wrappers.forEach((wrapper) => {
    wrapper.setAttribute('aria-label', label)
    wrapper.setAttribute('role', 'region')
    wrapper.tabIndex = 0
  })
  return wrappers.length
}
