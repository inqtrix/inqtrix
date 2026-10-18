import { canvasTabKey, type CanvasViewDescriptor } from './types'

/**
 * Pane transition key of the ACTIVE canvas descriptor. Tab switches
 * cross-fade in the host; run-INTERNAL navigation (overview <-> task
 * detail) deliberately shares ONE key: the run view owns that drill-in
 * as a page push with a permanently mounted list layer (scroll and
 * focus survive), so the host must not unmount the pane across it.
 */
export function canvasTransitionKey(
  descriptor: CanvasViewDescriptor | null,
): string {
  if (!descriptor) return 'empty'
  if (descriptor.view === 'run') {
    return `run:${descriptor.runId}`
  }
  return `${canvasTabKey(descriptor)}:${JSON.stringify(descriptor)}`
}
