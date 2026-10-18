import { describe, expect, it } from 'vitest'

import {
  answerCitationLabels,
  linkifyAgentArtifactCitations,
} from './artifactCitations'

/**
 * The streamed answer and the settled answer must render the SAME
 * markdown.
 *
 * A body streamed with plain `[W1]` and rewritten wholesale the moment
 * the answer settles would turn every citation into a link in one step,
 * which reads as the message being re-inserted. The server announces the
 * labels with `answer.started`, and the linkifier needs only labels, so
 * both sides produce identical output.
 */
describe('citations survive the writing -> ready transition unchanged', () => {
  const body = 'Die Pflicht gilt ab August 2026 [W1] und betrifft [W2].'

  it('produces the same markdown from announced labels as from refs', () => {
    const whileWriting = linkifyAgentArtifactCitations(
      body,
      [{ label: 'W1' }, { label: 'W2' }] as never,
    )
    const whenReady = linkifyAgentArtifactCitations(
      body,
      [
        { label: 'W1', url: 'https://example.org/a' },
        { label: 'W2', url: 'https://example.org/b' },
      ] as never,
    )
    expect(whileWriting).toBe(whenReady)
  })

  it('actually links the labels, so the guard is not vacuous', () => {
    const linked = linkifyAgentArtifactCitations(
      body,
      [{ label: 'W1' }, { label: 'W2' }] as never,
    )
    expect(linked).not.toBe(body)
    expect(linked).toContain('W1')
  })

  it('leaves an unannounced label alone', () => {
    // A label the answer does not actually cite must not become a link
    // just because it looks like one.
    const linked = linkifyAgentArtifactCitations(
      body,
      [{ label: 'W1' }] as never,
    )
    expect(linked).toContain('[W2]')
  })

  it('changes nothing when no labels were announced', () => {
    // A citation-free answer settles with no transition at all.
    expect(linkifyAgentArtifactCitations(body, [])).toBe(body)
  })
})

describe('answerCitationLabels', () => {
  const refs = [{ label: 'W1' }, { label: 'W2' }] as never

  it('uses the announced labels while the answer streams', () => {
    // The regression: this returned nothing while writing, so the body
    // carried plain `[W1]` and was rewritten wholesale on settle.
    expect(answerCitationLabels(true, ['W1', 'W2'], [])).toEqual([
      { label: 'W1' },
      { label: 'W2' },
    ])
  })

  it('uses the real references once the answer settled', () => {
    expect(answerCitationLabels(false, ['W9'], refs)).toEqual(refs)
  })

  it('has nothing to render when the server announced nothing', () => {
    // Older servers send no labels: the surface degrades to the previous
    // behaviour instead of inventing links.
    expect(answerCitationLabels(true, undefined, refs)).toEqual([])
  })
})
