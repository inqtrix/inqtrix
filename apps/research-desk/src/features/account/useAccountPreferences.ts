/**
 * Account-preferences server sync (account tier).
 *
 * Unlike the project-persistence hooks, this is NOT project data and is NOT
 * part of the project import: a user's theme / locale / contrast / bubble tone
 * follow the USER across devices, in their own per-user server row. "Account wins on
 * login" — on mount (a real per-user session) the saved preferences are
 * fetched and APPLIED over whatever the device held, then every change is
 * pushed. A user who has never saved (404) keeps their local/default until the
 * first change, which creates the row.
 *
 * Gated on a real per-user session (OIDC / local, authenticated) — never
 * anonymous/apikey, where every caller shares one ``__anonymous__`` /
 * ``__static__`` row and syncing would let one visitor's theme clobber others'.
 * The live preferences source is the ThemeProvider + locale (localStorage); the
 * caller passes the current snapshot plus an ``applyPreferences`` callback that
 * pushes a fetched row back into those setters.
 */

import { useCallback, useEffect, useRef, useState } from 'react'

import {
  getAccountPreferences,
  saveAccountPreferences,
} from '@/api/inqtrixClient'
import {
  preferencesFingerprint,
  preferencesFromServer,
  serverAccountPreferencesPayload,
} from '@/features/account/accountPreferencesSync'
import type { ProjectPreferences } from '@/features/project/types'
import { messageFromError } from '@/api/requestErrorText'

const AUTOSAVE_DEBOUNCE_MS = 1_500

type UseAccountPreferencesOptions = {
  apiKey: string | undefined
  /** Push a fetched server row into the live theme/locale setters. */
  applyPreferences: (preferences: ProjectPreferences) => void
  /** The current live preferences (ThemeProvider + locale snapshot). */
  preferences: ProjectPreferences
  /** Durable capability AND a real per-user (cookie) session, not demo. */
  syncActive: boolean
  workspaceId: string
}

export type AccountPreferencesHandle = {
  error: string | null
}

export function useAccountPreferences({
  apiKey,
  applyPreferences,
  preferences,
  syncActive,
  workspaceId,
}: UseAccountPreferencesOptions): AccountPreferencesHandle {
  const [error, setError] = useState<string | null>(null)
  const [hydrated, setHydrated] = useState(false)

  const preferencesRef = useRef(preferences)
  preferencesRef.current = preferences
  const applyRef = useRef(applyPreferences)
  applyRef.current = applyPreferences
  const optionsRef = useRef({ apiKey, workspaceId })
  optionsRef.current = { apiKey, workspaceId }
  const nowSecondsRef = useRef(() => Math.floor(Date.now() / 1000))

  // Last successfully-synced fingerprint; undefined until hydrated.
  const syncedRef = useRef<string | undefined>(undefined)
  const hydratedRef = useRef(false)
  const flushingRef = useRef(false)
  const flushPendingRef = useRef(false)
  const syncActiveRef = useRef(syncActive)
  syncActiveRef.current = syncActive

  const flush = useCallback(async () => {
    if (!syncActiveRef.current || !hydratedRef.current) return
    if (flushingRef.current) {
      flushPendingRef.current = true
      return
    }
    flushingRef.current = true
    try {
      const current = preferencesRef.current
      const fingerprint = preferencesFingerprint(current)
      if (fingerprint !== syncedRef.current) {
        await saveAccountPreferences(
          serverAccountPreferencesPayload(current, nowSecondsRef.current()),
          optionsRef.current,
        )
        syncedRef.current = fingerprint
      }
      setError(null)
    } catch (caught) {
      setError(messageFromError(caught))
    } finally {
      flushingRef.current = false
      if (flushPendingRef.current) {
        flushPendingRef.current = false
        void flush()
      }
    }
  }, [])

  // -- hydrate (account wins on login) ---------------------------------- #

  useEffect(() => {
    if (!syncActive || hydratedRef.current) return
    let cancelled = false
    void (async () => {
      try {
        const server = await getAccountPreferences(optionsRef.current)
        if (cancelled) return
        if (server) {
          // Account wins: apply the saved row over the device's preferences,
          // and seed synced to it so the resulting local change does not echo
          // back as a redundant push.
          const applied = preferencesFromServer(server, preferencesRef.current)
          applyRef.current(applied)
          syncedRef.current = preferencesFingerprint(applied)
        } else {
          // Never saved: keep the local/default preferences; the first change
          // will create the row (seed synced to the current local snapshot so
          // an unchanged session does not push).
          syncedRef.current = preferencesFingerprint(preferencesRef.current)
        }
        hydratedRef.current = true
        setHydrated(true)
        setError(null)
      } catch (caught) {
        if (!cancelled) setError(messageFromError(caught))
      }
    })()
    return () => {
      cancelled = true
    }
  }, [syncActive])

  // -- debounced autosave trigger --------------------------------------- #

  useEffect(() => {
    if (!syncActive || !hydrated) return
    const timer = setTimeout(() => {
      void flush()
    }, AUTOSAVE_DEBOUNCE_MS)
    return () => clearTimeout(timer)
  }, [preferences, syncActive, hydrated, flush])

  // -- re-arm when the session ends ------------------------------------- #

  useEffect(() => {
    if (syncActive) return
    hydratedRef.current = false
    setHydrated(false)
    syncedRef.current = undefined
  }, [syncActive])

  return { error }
}
