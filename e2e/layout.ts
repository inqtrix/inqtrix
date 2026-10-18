export type Bounds = {
  bottom: number
  left: number
  right: number
  top: number
}

export type ControlGeometry = {
  bounds: Bounds
  name: string
}

export type Viewport = {
  height: number
  width: number
}

export function controlBoundsViolations(
  controls: ControlGeometry[],
  scope: Bounds | null,
  viewport: Viewport,
): string[] {
  if (!scope) return []
  const viewportBounds = { bottom: viewport.height, left: 0, right: viewport.width, top: 0 }
  return controls
    .filter((control) => (
      !contained(control.bounds, viewportBounds)
      || !contained(control.bounds, scope)
    ))
    .map((control) => control.name)
}

function contained(inner: Bounds, outer: Bounds): boolean {
  return inner.left >= outer.left - 1
    && inner.right <= outer.right + 1
    && inner.top >= outer.top - 1
    && inner.bottom <= outer.bottom + 1
}
