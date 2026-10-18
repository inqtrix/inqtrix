import { readFileSync } from 'node:fs'

import { describe, expect, it } from 'vitest'

import {
  secretClipboardText,
  uncopiedParts,
  type SecretPart,
} from './EditorGuestLinksSection'

const COPY = {
  copyHint: 'Link und Passwort getrennt übermitteln.',
  password: 'Passwort',
  url: 'Link',
}

function copied(...parts: SecretPart[]): ReadonlySet<SecretPart> {
  return new Set(parts)
}

describe('one-time guest secrets', () => {
  // Link and password are shown exactly once and are irreversible when
  // lost. The dialog asks before closing over them, so this predicate
  // decides whether a real warning appears.
  it('counts a freshly created link as uncopied in both halves', () => {
    const secret = { label: 'AB12', password: 'A-B', url: 'https://x/s/t' }
    expect(uncopiedParts(secret, copied())).toEqual(['url', 'password'])
  })

  it('still warns while only one half has been captured', () => {
    const secret = { label: 'AB12', password: 'A-B', url: 'https://x/s/t' }
    expect(uncopiedParts(secret, copied('url'))).toEqual(['password'])
    expect(uncopiedParts(secret, copied('password'))).toEqual(['url'])
  })

  it('stops warning once every half the card shows is captured', () => {
    const both = { label: 'AB12', password: 'A-B', url: 'https://x/s/t' }
    expect(uncopiedParts(both, copied('password', 'url'))).toEqual([])
    // A rotation shows ONE half; the other must not keep the warning alive.
    const passwordOnly = { label: 'AB12', password: 'A-B' }
    expect(uncopiedParts(passwordOnly, copied('password'))).toEqual([])
    const urlOnly = { label: 'AB12', url: 'https://x/s/t' }
    expect(uncopiedParts(urlOnly, copied('url'))).toEqual([])
  })

  it('carries the separate-delivery reminder into the clipboard text', () => {
    const text = secretClipboardText(
      { label: 'AB12', password: 'A-B-C', url: 'https://x/s/t' },
      COPY,
    )
    expect(text).toContain('https://x/s/t')
    expect(text).toContain('A-B-C')
    // The card advises sending the halves separately; one clipboard entry
    // works against that, so the advice must travel with the text.
    expect(text).toContain(COPY.copyHint)
  })
})

describe('the dialog guards every one of its close paths', () => {
  // Bound stated: this reads the SOURCE. ShareDialog is tested through
  // renderToStaticMarkup here, which cannot click a backdrop, press Escape
  // or fire window.confirm.
  //
  // The third path is the one that is easy to miss: Escape does not live in
  // this file at all — the shared focus trap fires it through the `onClose`
  // it is handed. Passing the raw callback there leaves a silent hole.
  const source = readFileSync(
    'src/features/sharing/ShareDialog.tsx',
    'utf8',
  )

  it('routes button, backdrop AND Escape through the guard', () => {
    expect(source).toContain('onClick={closeGuarded}')
    expect(source).toContain(
      'if (event.target === event.currentTarget) closeGuarded()',
    )
    expect(source).toContain('onClose: closeGuarded,')
    // No JSX handler reaches onClose directly.
    expect(source).not.toContain('onClick={onClose}')
    // Exactly two callers remain: the guard's own tail, and submitLeave —
    // which only recipients reach, and guest links never render for those.
    expect(source.split('onClose()').length - 1).toBe(2)
  })

  it('keeps a failed mutation visible instead of reloading over it', () => {
    // `reload()` ends in 'ready'. Setting the error before it silently
    // erases the banner, so a 500 would look like nothing had happened.
    const section = readFileSync(
      'src/features/sharing/EditorGuestLinksSection.tsx',
      'utf8',
    )
    // Anchored on `mutate` — `create` has its own catch without a reload,
    // so slicing from the first `catch` measures the wrong block.
    const mutate = section.slice(section.indexOf('const mutate = async ('))
    const reloadAt = mutate.indexOf('await reload()')
    const errorAt = mutate.indexOf("setStatus('error')")
    expect(reloadAt).toBeGreaterThan(-1)
    expect(errorAt).toBeGreaterThan(-1)
    expect(reloadAt).toBeLessThan(errorAt)
  })

  it('drops the card when its own link is ended', () => {
    const section = readFileSync(
      'src/features/sharing/EditorGuestLinksSection.tsx',
      'utf8',
    )
    const revoke = section.slice(
      section.indexOf("if (operation === 'revoke')"),
      section.indexOf("} else if (operation === 'rotate')"),
    )
    expect(revoke).toContain('setSecret(null)')
  })

  it('asks only while something is actually uncopied', () => {
    expect(source).toContain('if (uncopiedSecret && !window.confirm(')
    expect(source).toContain('onUncopiedSecretChange={setUncopiedSecret}')
  })
})
