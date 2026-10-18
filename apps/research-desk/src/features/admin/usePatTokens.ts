import { useCallback, useEffect, useRef, useState } from 'react'

import {
  type AccessToken,
  createAccessToken,
  listAccessTokens,
  revokeAccessToken,
} from '@/api/inqtrixClient'
import { seedAccessTokens } from './demo'

type PatStatus = 'idle' | 'loading' | 'ready' | 'error'

export type PatTokensState = {
  available: boolean
  demo: boolean
  tokens: AccessToken[]
  status: PatStatus
  error: string | null
  mutationError: string | null
}

function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

/**
 * Personal-access-token management (the caller's own tokens; session-only
 * server-side). Same hook shape as the user admin hook. `createToken`
 * returns the one-time plaintext so the panel can reveal it exactly once;
 * the demo branch fabricates a deterministic token so the reveal flow is
 * exercisable offline.
 */
export function usePatTokens({
  demo,
  enabled,
}: {
  demo: boolean
  enabled: boolean
}) {
  const [state, setState] = useState<PatTokensState>({
    available: false,
    demo,
    error: null,
    mutationError: null,
    status: 'idle',
    tokens: [],
  })
  const generationRef = useRef(0)

  const reload = useCallback(async () => {
    // Bump the generation on EVERY reload (incl. the demo/disabled early
    // returns), so a live fetch started before a demo toggle cannot resolve
    // and clobber the seeded/empty state with stale rows.
    const generation = ++generationRef.current
    if (!enabled) {
      setState({
        available: false,
        demo,
        error: null,
        mutationError: null,
        status: 'idle',
        tokens: [],
      })
      return
    }
    if (demo) {
      setState({
        available: true,
        demo,
        error: null,
        mutationError: null,
        status: 'ready',
        tokens: seedAccessTokens(),
      })
      return
    }
    setState((current) => ({ ...current, available: true, status: 'loading' }))
    try {
      const { tokens } = await listAccessTokens()
      if (generationRef.current !== generation) return
      setState({
        available: true,
        demo,
        error: null,
        mutationError: null,
        status: 'ready',
        tokens,
      })
    } catch (error) {
      if (generationRef.current !== generation) return
      setState((current) => ({
        ...current,
        available: true,
        error: messageOf(error),
        // Same contract as the user directory: a failed refresh keeps the
        // rows it already has instead of claiming the list is empty.
        status: 'error',
      }))
    }
  }, [demo, enabled])

  useEffect(() => {
    void reload()
  }, [reload])

  /**
   * Mint a token and return the one-time plaintext. REthrows on failure so
   * the create dialog can show the inline error (e.g. 409 token-limit).
   */
  const createToken = useCallback(
    async (input: {
      name: string
      expiresInDays?: number
    }): Promise<{ token: string; tokenId: string }> => {
      if (demo) {
        const slug = input.name.toLowerCase().replace(/[^a-z0-9]+/g, '-')
        const tokenId = `demo-pat-${slug || 'token'}`
        const token = `inq_demo_${slug || 'token'}_0a1b2c3d4e5f6071`
        setState((current) => ({
          ...current,
          tokens: [
            {
              token_id: tokenId,
              name: input.name,
              created_at: 1_767_225_600,
              expires_at: input.expiresInDays
                ? 1_767_225_600 + input.expiresInDays * 86_400
                : null,
              last_used_at: null,
              scopes: [],
            },
            ...current.tokens,
          ],
        }))
        return { token, tokenId }
      }
      const created = await createAccessToken(input)
      await reload()
      return { token: created.token, tokenId: created.token_id }
    },
    [demo, reload],
  )

  const revokeToken = useCallback(
    async (tokenId: string) => {
      if (demo) {
        setState((current) => ({
          ...current,
          tokens: current.tokens.filter((token) => token.token_id !== tokenId),
        }))
        return
      }
      setState((current) => ({ ...current, mutationError: null }))
      try {
        await revokeAccessToken(tokenId)
        await reload()
      } catch (error) {
        await reload()
        setState((current) => ({ ...current, mutationError: messageOf(error) }))
      }
    },
    [demo, reload],
  )

  return { createToken, reload, revokeToken, state }
}
