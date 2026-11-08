import { readFileSync } from 'node:fs'

import { describe, expect, it } from 'vitest'

describe('notes collected on their cards show in the assistant input', () => {
  // "Sammeln" marks an AI note as collected; the input must show it. With the
  // list of attached notes rendered for "attach all" alone, only the send
  // button became active.
  it('lists the attached notes whenever there are any, not only for "attach all"', () => {
    const source = readFileSync('src/features/editor/EditorWorkspace.tsx', 'utf8')
    expect(source).toContain('{isAttachActive || attachedComments.length > 0 ? (')
    // The bare form is what hid a note collected one by one.
    expect(source).not.toContain('{isAttachActive ? (')
  })
})
