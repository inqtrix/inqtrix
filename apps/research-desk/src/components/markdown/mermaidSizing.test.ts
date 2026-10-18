import { describe, expect, it } from 'vitest'

import { mermaidNaturalWidth, mermaidPreviewMaxWidth } from './mermaidSizing'

describe('Mermaid viewer sizing', () => {
  it('modestly enlarges the rendered viewBox instead of forcing compact diagrams full-width', () => {
    const svg = '<svg viewBox="0 0 377.94140625 422.5"></svg>'

    expect(mermaidNaturalWidth(svg)).toBe(378)
    expect(mermaidPreviewMaxWidth(svg)).toBe(435)
  })

  it('supports negative viewBox origins and rejects invalid dimensions', () => {
    expect(mermaidPreviewMaxWidth('<svg viewBox="-50 -10 1345 551"></svg>')).toBe(1547)
    expect(mermaidNaturalWidth('<svg viewBox="-50 -10 1345 551"></svg>')).toBe(1345)
    expect(mermaidPreviewMaxWidth('<svg viewBox="0 0 0 100"></svg>')).toBeUndefined()
    expect(mermaidPreviewMaxWidth('<svg></svg>')).toBeUndefined()
  })
})
