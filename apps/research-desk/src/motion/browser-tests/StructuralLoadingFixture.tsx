import { StrictMode, useEffect, useState } from 'react'
import { createRoot } from 'react-dom/client'

import { Skeleton } from '@/components/ui/skeleton'
import {
  StructuralLoadBoundary,
  useStructuralRenderBlocker,
} from '@/motion/StructuralLoadBoundary'
import '@/styles/globals.css'

const parameters = new URLSearchParams(window.location.search)
const readyAfterMs = Number(parameters.get('readyAfterMs') ?? 50)
const blockerAfterMs = Number(parameters.get('blockerAfterMs') ?? 0)

function GeometryTarget({ blockerMs }: { blockerMs: number }) {
  const [pending, setPending] = useState(blockerMs > 0)
  useStructuralRenderBlocker(pending)

  useEffect(() => {
    if (blockerMs <= 0) return undefined
    const timer = window.setTimeout(() => setPending(false), blockerMs)
    return () => window.clearTimeout(timer)
  }, [blockerMs])

  return (
    <div className="h-full bg-background p-8" data-fixture-target="">
      <h1 className="text-xl font-semibold">Ready target</h1>
      <div className="mt-4 h-64 rounded-lg border border-border bg-card" />
    </div>
  )
}

function StructuralLoadingFixture() {
  const [ready, setReady] = useState(false)
  useEffect(() => {
    const timer = window.setTimeout(() => setReady(true), readyAfterMs)
    return () => window.clearTimeout(timer)
  }, [])

  return (
    <main className="h-screen bg-background p-6 text-foreground">
      <StructuralLoadBoundary
        className="h-full"
        fallback={(
          <div className="flex h-full flex-col gap-4 bg-background p-8" data-fixture-skeleton="">
            <Skeleton className="h-7 w-1/3" />
            <Skeleton className="h-4 w-5/6" />
            <Skeleton className="h-4 w-3/4" />
            <Skeleton className="min-h-64 flex-1 rounded-lg" />
          </div>
        )}
        identity="fixture:target"
        phase={ready ? 'ready' : 'pending'}
      >
        <GeometryTarget blockerMs={blockerAfterMs} />
      </StructuralLoadBoundary>
    </main>
  )
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <StructuralLoadingFixture />
  </StrictMode>,
)
