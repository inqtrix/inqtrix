import { useEffect, type RefObject } from 'react'

import { scheduleIdle } from '@/lib/idle'

const MARKDOWN_WARMUP_MARGIN_PX = 1200

type ProgressiveMarkdownWorkOptions = {
  /** Run immediately when a concealed surface is waiting on this work. */
  eager?: boolean
  isReady: boolean
  run: () => void
  targetRef: RefObject<HTMLElement | null>
  workKey: string
}

export function useProgressiveMarkdownWork({
  eager = false,
  isReady,
  run,
  targetRef,
  workKey,
}: ProgressiveMarkdownWorkOptions): void {
  useEffect(() => {
    if (isReady) return undefined

    if (eager) {
      run()
      return undefined
    }

    const target = targetRef.current
    if (!target || typeof IntersectionObserver === 'undefined') {
      run()
      return undefined
    }

    const root = target.closest<HTMLElement>('[data-scroll-area-viewport]')
    let cancelIdle: (() => void) | null = null
    let disposed = false
    let hasStarted = false

    const runNow = () => {
      if (disposed || hasStarted) return
      hasStarted = true
      cancelIdle?.()
      cancelIdle = null
      run()
    }
    const runWhenIdle = () => {
      if (disposed || hasStarted || cancelIdle) return
      cancelIdle = scheduleIdle(() => {
        cancelIdle = null
        runNow()
      }, { timeout: 600 })
    }

    const visibleObserver = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) runNow()
      },
      { root },
    )
    const nearObserver = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) runWhenIdle()
      },
      { root, rootMargin: `${MARKDOWN_WARMUP_MARGIN_PX}px 0px` },
    )

    visibleObserver.observe(target)
    nearObserver.observe(target)
    return () => {
      disposed = true
      cancelIdle?.()
      visibleObserver.disconnect()
      nearObserver.disconnect()
    }
  }, [eager, isReady, run, targetRef, workKey])
}
