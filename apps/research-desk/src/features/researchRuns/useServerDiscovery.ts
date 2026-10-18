import { useEffect, useState } from 'react'
import {
  fetchCapabilities,
  fetchHealth,
  fetchStacks,
  hasHttpStatus,
} from '@/api/inqtrixClient'
import type {
  InqtrixCapabilities,
  InqtrixHealth,
  InqtrixStack,
  InqtrixStackList,
  StackDiscoveryStatus,
} from './types'
import { messageFromError } from '@/api/requestErrorText'

type UseServerDiscoveryOptions = {
  enabled: boolean
}

type StackDiscoveryCacheEntry = {
  payload?: InqtrixStackList
  promise?: Promise<InqtrixStackList | null>
  status: StackDiscoveryStatus
}

const stackDiscoveryCache = new Map<string, StackDiscoveryCacheEntry>()
const STACK_DISCOVERY_CACHE_KEY = 'default'

/**
 * Workspace-INDEPENDENT server discovery: health, the capability manifest, and
 * the stack catalog. Split out of {@link useResearchRunApi} because these probes
 * gate the auth/namespace resolution (cookie mode is derived from `health`),
 * which in turn produces the per-user workspace namespace that run operations
 * scope to. Resolving discovery in its own hook keeps that ordering linear --
 * discovery -> auth -> namespace -> run ops -- so the run hook can receive the
 * already-resolved namespace instead of re-probing the session itself (which
 * left a window where a run could be scoped to the browser id before the
 * namespace resolved).
 *
 * None of these endpoints are workspace-scoped, so the hook takes no
 * `workspaceId`. Failed discovery retries after five seconds, without a page
 * reload or overlapping attempts. A 404 from the capability manifest denotes
 * an older backend; it is not a transport failure and does not cause retries.
 */
export function useServerDiscovery({ enabled }: UseServerDiscoveryOptions) {
  const [health, setHealth] = useState<InqtrixHealth | null>(null)
  const [capabilities, setCapabilities] = useState<InqtrixCapabilities | null>(null)
  const [defaultStackName, setDefaultStackName] = useState<string | null>(null)
  const [stackNames, setStackNames] = useState<string[]>([])
  const [stackDiscoveryStatus, setStackDiscoveryStatus] = useState<StackDiscoveryStatus>('unknown')
  const [stacks, setStacks] = useState<InqtrixStack[]>([])
  const [lastError, setLastError] = useState<string | null>(null)
  // `true` once the health probe has settled (success OR failure), so the auth
  // mode is determinable. Consumers gate run-listing on this: until health
  // resolves, `health` is null and a naive auth-mode read would default to
  // `none` and list prematurely under the browser id.
  const [ready, setReady] = useState(false)

  useEffect(() => {
    if (!enabled) {
      setHealth(null)
      setCapabilities(null)
      setDefaultStackName(null)
      setStackNames([])
      setStackDiscoveryStatus('unknown')
      setStacks([])
      setLastError(null)
      setReady(false)
      return undefined
    }

    let ignore = false

    async function discover() {
      let discoveryError: string | null = null
      // Health and capabilities are independent probes — only the stack
      // listing below needs the capability manifest. Awaiting them one
      // after the other made the shell wait two round-trips where one
      // suffices; on a remote connection that is the difference between
      // a brief and a noticeable blank screen. State is still applied in
      // the original order so error precedence and the `ready` moment
      // are unchanged.
      const [healthResult, capabilitiesResult] = await Promise.allSettled([
        fetchHealth(),
        fetchCapabilities(),
      ])

      if (healthResult.status === 'fulfilled') {
        if (!ignore) setHealth(healthResult.value)
      } else if (!ignore) {
        discoveryError = messageFromError(healthResult.reason)
      }
      // Health has now been probed (ok or failed): the auth mode is
      // determinable, so listing may proceed once the caller is admitted.
      if (!ignore) setReady(true)

      let capabilitiesSupportsStacks = false
      if (capabilitiesResult.status === 'fulfilled') {
        capabilitiesSupportsStacks = Boolean(
          capabilitiesResult.value?.features?.multi_stack,
        )
        if (!ignore) setCapabilities(capabilitiesResult.value)
      } else if (!ignore) {
        // 404 = older backend without the capability manifest; every
        // capability-gated affordance simply stays hidden. Anything
        // else is a real error and surfaces like the other probes.
        setCapabilities(null)
        if (!hasHttpStatus(capabilitiesResult.reason, 404)) {
          discoveryError = messageFromError(capabilitiesResult.reason)
        }
      }

      try {
        // Ask the manifest instead of probing: a single-stack server does
        // not mount GET /v1/stacks at all.
        const stackPayload = capabilitiesSupportsStacks
          ? await discoverStacks()
          : null
        if (!ignore) {
          if (stackPayload) {
            setDefaultStackName(stackPayload.default)
            setStackNames(stackPayload.stacks.map((stack) => stack.name))
            setStackDiscoveryStatus('available')
            setStacks(stackPayload.stacks)
          } else {
            setDefaultStackName(null)
            setStackNames([])
            setStackDiscoveryStatus('unsupported')
            setStacks([])
          }
        }
      } catch (error) {
        if (!ignore) {
          setDefaultStackName(null)
          setStackNames([])
          setStackDiscoveryStatus('unknown')
          setStacks([])
          discoveryError = messageFromError(error)
        }
      }
      if (!ignore) setLastError(discoveryError)
      return discoveryError === null
    }

    const stop = retryServerDiscovery(discover)

    return () => {
      ignore = true
      stop()
    }
  }, [enabled])

  return {
    capabilities,
    defaultStackName,
    health,
    lastError,
    ready,
    stackDiscoveryStatus,
    stackNames,
    stacks,
  }
}

/** Retry only a failed discovery, after its requests have all settled. */
export function retryServerDiscovery(discover: () => Promise<boolean>): () => void {
  let stopped = false
  let timer: ReturnType<typeof setTimeout> | undefined
  async function attempt() {
    const success = await discover()
    if (!stopped && !success) timer = setTimeout(() => { void attempt() }, 5000)
  }
  void attempt()
  return () => {
    stopped = true
    clearTimeout(timer)
  }
}

async function discoverStacks() {
  const cached = stackDiscoveryCache.get(STACK_DISCOVERY_CACHE_KEY)
  if (cached?.status === 'unsupported') return null
  if (cached?.payload) return cached.payload
  if (cached?.promise) return cached.promise

  const entry: StackDiscoveryCacheEntry = {
    status: 'unknown',
  }
  entry.promise = fetchStacks()
    .then((payload) => {
      entry.payload = payload
      entry.status = 'available'
      return payload
    })
    .catch((error) => {
      stackDiscoveryCache.delete(STACK_DISCOVERY_CACHE_KEY)
      throw error
    })
    .finally(() => {
      entry.promise = undefined
    })
  stackDiscoveryCache.set(STACK_DISCOVERY_CACHE_KEY, entry)
  return entry.promise
}
