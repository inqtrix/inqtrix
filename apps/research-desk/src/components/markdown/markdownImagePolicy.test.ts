import { describe, expect, it } from 'vitest'

import { classifyMarkdownImageSource } from './markdownImagePolicy'

const BASE_HREF = 'https://desk.inqtrix.test/workspaces/demo'

describe('classifyMarkdownImageSource', () => {
  it('loads relative and same-origin images directly', () => {
    expect(classifyMarkdownImageSource('/assets/chart.png', BASE_HREF)).toEqual({
      kind: 'direct',
      src: '/assets/chart.png',
    })
    expect(classifyMarkdownImageSource('https://desk.inqtrix.test/chart.png', BASE_HREF)).toEqual({
      kind: 'direct',
      src: 'https://desk.inqtrix.test/chart.png',
    })
  })

  it('requires approval for external and protocol-relative images', () => {
    expect(classifyMarkdownImageSource('https://media.example.com/chart.png', BASE_HREF)).toEqual({
      host: 'media.example.com',
      kind: 'external',
      src: 'https://media.example.com/chart.png',
    })
    expect(classifyMarkdownImageSource('//cdn.example.com/chart.png', BASE_HREF)).toEqual({
      host: 'cdn.example.com',
      kind: 'external',
      src: 'https://cdn.example.com/chart.png',
    })
  })

  it('rejects missing, malformed, and unsafe image sources', () => {
    expect(classifyMarkdownImageSource(undefined, BASE_HREF)).toEqual({ kind: 'invalid' })
    expect(classifyMarkdownImageSource('javascript:alert(1)', BASE_HREF)).toEqual({ kind: 'invalid' })
    expect(classifyMarkdownImageSource('https://[invalid', BASE_HREF)).toEqual({ kind: 'invalid' })
  })
})
