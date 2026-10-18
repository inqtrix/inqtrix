import { useEffect, useState } from 'react'

export function useMediaQuery(query: string) {
  const [matches, setMatches] = useState(() => {
    if (typeof window === 'undefined') return false
    return window.matchMedia(query).matches
  })

  useEffect(() => {
    const mediaQueryList = window.matchMedia(query)
    // The breakpoint flip must be an URGENT update: deferring it (startTransition)
    // lets a busy render loop (demo ticker, live run) starve the transition, so
    // the desktop layout lingers at mobile widths — the collapsing right panel
    // stays a squished sliver instead of becoming a drawer.
    const updateMatches = () => setMatches(mediaQueryList.matches)

    updateMatches()
    mediaQueryList.addEventListener('change', updateMatches)

    return () => mediaQueryList.removeEventListener('change', updateMatches)
  }, [query])

  return matches
}
