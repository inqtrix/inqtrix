import {
  useEffect,
  useLayoutEffect,
  useRef,
  type RefObject,
} from 'react'
import type { PanelImperativeHandle } from 'react-resizable-panels'

const PANEL_MOTION_CLASS_NAME = 'inqtrix-resizable-panel-motion'
const PANEL_MOTION_DURATION_MS = 260
const PANEL_MOTION_SETTLE_MS = PANEL_MOTION_DURATION_MS + 80
const PANEL_PROGRAMMATIC_SETTLE_MS = 80

type AnimatedResizablePanelCollapseOptions = {
  expanded: boolean
  expandedSize: number
  reduceMotion?: boolean | null
}

type AnimatedResizablePanelCollapse = {
  groupRef: RefObject<HTMLDivElement | null>
  isProgrammaticLayoutChange: () => boolean
  panelRef: RefObject<PanelImperativeHandle | null>
}

export function useAnimatedResizablePanelCollapse({
  expanded,
  expandedSize,
  reduceMotion,
}: AnimatedResizablePanelCollapseOptions): AnimatedResizablePanelCollapse {
  const groupRef = useRef<HTMLDivElement | null>(null)
  const panelRef = useRef<PanelImperativeHandle | null>(null)
  const didSyncInitialStateRef = useRef(false)
  const expandedSizeRef = useRef(expandedSize)
  const motionFrameRef = useRef<number | null>(null)
  const motionTimeoutRef = useRef<number | null>(null)
  const previousExpandedRef = useRef(expanded)
  const programmaticLayoutChangeRef = useRef(false)
  const programmaticLayoutTimeoutRef = useRef<number | null>(null)

  useEffect(() => {
    expandedSizeRef.current = expandedSize
  }, [expandedSize])

  useLayoutEffect(() => {
    const panel = panelRef.current
    if (!panel) {
      // The panel can be unmounted (mobile breakpoint) while `expanded` still
      // changes; record it so the first toggle after remount animates from the
      // correct previous state instead of being skipped as a no-op.
      previousExpandedRef.current = expanded
      return
    }

    const isInitialSync = !didSyncInitialStateRef.current
    didSyncInitialStateRef.current = true
    const shouldAnimate = !isInitialSync && !reduceMotion && previousExpandedRef.current !== expanded
    const group = groupRef.current

    if (motionFrameRef.current != null) {
      window.cancelAnimationFrame(motionFrameRef.current)
      motionFrameRef.current = null
    }
    if (motionTimeoutRef.current != null) {
      window.clearTimeout(motionTimeoutRef.current)
      motionTimeoutRef.current = null
    }
    if (programmaticLayoutTimeoutRef.current != null) {
      window.clearTimeout(programmaticLayoutTimeoutRef.current)
      programmaticLayoutTimeoutRef.current = null
    }

    const clearMotionClass = () => {
      group?.classList.remove(PANEL_MOTION_CLASS_NAME)
    }

    programmaticLayoutChangeRef.current = true

    const syncPanelState = () => {
      if (expanded) {
        panel.expand()
        panel.resize(`${expandedSizeRef.current}%`)
        return
      }
      panel.resize('0%')
      panel.collapse()
    }

    const clearProgrammaticLayoutChange = (delayMs: number) => {
      programmaticLayoutTimeoutRef.current = window.setTimeout(() => {
        programmaticLayoutChangeRef.current = false
        programmaticLayoutTimeoutRef.current = null
      }, delayMs)
    }

    if (shouldAnimate) {
      group?.classList.add(PANEL_MOTION_CLASS_NAME)
      motionFrameRef.current = window.requestAnimationFrame(() => {
        motionFrameRef.current = null
        syncPanelState()
      })
      motionTimeoutRef.current = window.setTimeout(() => {
        clearMotionClass()
        clearProgrammaticLayoutChange(0)
        motionTimeoutRef.current = null
      }, PANEL_MOTION_SETTLE_MS)
    } else {
      clearMotionClass()
      syncPanelState()
      clearProgrammaticLayoutChange(PANEL_PROGRAMMATIC_SETTLE_MS)
    }

    previousExpandedRef.current = expanded
  }, [expanded, reduceMotion])

  useEffect(() => () => {
    if (motionFrameRef.current != null) {
      window.cancelAnimationFrame(motionFrameRef.current)
    }
    if (motionTimeoutRef.current != null) {
      window.clearTimeout(motionTimeoutRef.current)
    }
    if (programmaticLayoutTimeoutRef.current != null) {
      window.clearTimeout(programmaticLayoutTimeoutRef.current)
    }
    programmaticLayoutChangeRef.current = false
    groupRef.current?.classList.remove(PANEL_MOTION_CLASS_NAME)
  }, [])

  return {
    groupRef,
    isProgrammaticLayoutChange: () => programmaticLayoutChangeRef.current,
    panelRef,
  }
}
