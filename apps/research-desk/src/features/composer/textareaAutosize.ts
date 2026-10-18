export function resizeTextareaToRows(
  textarea: HTMLTextAreaElement | null,
  maxRows?: number,
) {
  if (!textarea) return

  textarea.style.height = 'auto'

  const computedStyle = window.getComputedStyle(textarea)
  const fontSize = Number.parseFloat(computedStyle.fontSize) || 16
  const lineHeight = Number.parseFloat(computedStyle.lineHeight) || fontSize * 1.5
  const paddingTop = Number.parseFloat(computedStyle.paddingTop) || 0
  const paddingBottom = Number.parseFloat(computedStyle.paddingBottom) || 0
  const borderTop = Number.parseFloat(computedStyle.borderTopWidth) || 0
  const borderBottom = Number.parseFloat(computedStyle.borderBottomWidth) || 0
  const configuredMinHeight = Number.parseFloat(computedStyle.minHeight)
  const minHeight = Number.isFinite(configuredMinHeight)
    ? configuredMinHeight
    : lineHeight + paddingTop + paddingBottom + borderTop + borderBottom
  const maxHeight = maxRows === undefined
    ? Number.POSITIVE_INFINITY
    : (lineHeight * maxRows) + paddingTop + paddingBottom + borderTop + borderBottom
  const nextHeight = Math.min(textarea.scrollHeight + borderTop + borderBottom, maxHeight)
  const shouldScroll = textarea.scrollHeight > maxHeight
  const caretIsAtEnd = textarea.selectionEnd >= textarea.value.length

  textarea.style.height = `${Math.max(nextHeight, minHeight)}px`
  textarea.style.overflowY = shouldScroll ? 'auto' : 'hidden'

  if (shouldScroll && caretIsAtEnd) {
    textarea.scrollTop = textarea.scrollHeight
  }
}
