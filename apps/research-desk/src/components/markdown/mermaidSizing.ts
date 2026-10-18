const MERMAID_PREVIEW_SCALE = 1.15

/**
 * Reads the native width encoded by Mermaid's root SVG viewBox.
 */
export function mermaidNaturalWidth(svg: string): number | undefined {
  const viewBox = svg.match(/\bviewBox=["']([^"']+)["']/i)?.[1]
  if (!viewBox) return undefined

  const values = viewBox.trim().split(/[\s,]+/).map(Number)
  if (values.length !== 4) return undefined

  const width = values[2]
  if (!Number.isFinite(width) || width <= 0) return undefined

  return Math.ceil(width)
}

/**
 * Returns a modestly enlarged width for the Mermaid viewer while preserving
 * the type scale encoded in the rendered SVG. Inline figures use the unscaled
 * native width and therefore never enlarge compact diagrams.
 */
export function mermaidPreviewMaxWidth(svg: string): number | undefined {
  const width = mermaidNaturalWidth(svg)
  return width === undefined ? undefined : Math.ceil(width * MERMAID_PREVIEW_SCALE)
}
