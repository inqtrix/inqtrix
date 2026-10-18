import type { ComponentType } from 'react'
import type { CanvasViewDescriptor, CanvasViewKind } from './types'

/**
 * View registry of the polymorphic canvas host. Feature code (v1: the
 * Agent Desk) builds one registry per workspace with its renderers already
 * bound to feature data (closures over workspace props) — the host itself
 * stays agnostic: it only maps `descriptor.view` to a renderer. Later
 * suites (knowledge, research) can mount the SAME host with their own
 * registry entries through this named seam.
 */
export type CanvasViewRenderer<K extends CanvasViewKind = CanvasViewKind> =
  ComponentType<{
    descriptor: Extract<CanvasViewDescriptor, { view: K }>
  }>

export type CanvasViewRegistry = {
  [K in CanvasViewKind]?: CanvasViewRenderer<K>
}

export function resolveCanvasRenderer(
  registry: CanvasViewRegistry,
  descriptor: CanvasViewDescriptor,
): CanvasViewRenderer | null {
  return (registry[descriptor.view] as CanvasViewRenderer | undefined) ?? null
}
