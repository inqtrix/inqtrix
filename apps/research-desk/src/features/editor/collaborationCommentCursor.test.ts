import { readFileSync } from 'node:fs'

import { describe, expect, it } from 'vitest'

import { commentCursorAfterMutation } from './useCollaborationComments'

type ServerThread = { id: string; revision: number }

/** The incremental page the server answers: every thread changed after the
 * cursor, the same filter as the comment listing. */
function pageAfter(threads: readonly ServerThread[], cursor: number): string[] {
  return threads.filter((thread) => thread.revision > cursor).map((thread) => thread.id)
}

describe('the comment cursor after our own comment', () => {
  // Two people comment at the same moment from the view at revision 5. The
  // other person's thread commits first and takes 6, ours takes 7.
  const server: ServerThread[] = [
    { id: 'theirs', revision: 6 },
    { id: 'ours', revision: 7 },
  ]

  it('keeps the comment another person sent while we wrote ours', () => {
    const next = commentCursorAfterMutation(5, 7)

    expect(next).toEqual({ cursor: 5, missed: true })
    // The load the gap asks for, and every later one, still carries it.
    expect(pageAfter(server, next.cursor)).toContain('theirs')
  })

  it('moves to our comment when nothing came in between', () => {
    expect(commentCursorAfterMutation(5, 6)).toEqual({ cursor: 6, missed: false })
  })

  it('stays put for the answer to a replayed command', () => {
    expect(commentCursorAfterMutation(8, 6)).toEqual({ cursor: 8, missed: false })
  })
})

describe('adopting our own comment change (source pin)', () => {
  const hook = readFileSync(
    'src/features/editor/useCollaborationComments.ts',
    'utf8',
  )

  it('moves the cursor only as far as the gap rule allows and loads a gap', () => {
    const adopt = hook.slice(
      hook.indexOf('const adoptMutation = useCallback('),
      hook.indexOf('const createThread = useCallback('),
    )
    expect(adopt.length).toBeGreaterThan(100)
    expect(adopt).toContain('commentCursorAfterMutation(')
    expect(adopt).toContain('revisionRef.current = next.cursor')
    expect(adopt).toContain('if (next.missed) void requestRefresh(false)')
    expect(adopt).not.toContain('Math.max(')
  })
})
