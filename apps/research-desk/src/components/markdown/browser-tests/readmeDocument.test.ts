import { existsSync, readFileSync } from 'node:fs'
import path from 'node:path'

import { describe, expect, it } from 'vitest'

import { readmeArchitectureMarkdown, readmeNavigation } from './readmeDocument'

const repositoryRoot = path.resolve(__dirname, '../../../../../..')
const readme = readFileSync(path.join(repositoryRoot, 'README.md'), 'utf8')

describe('repository README', () => {
  it('resolves every local navigation target', () => {
    const { anchors, links } = readmeNavigation(readme)
    expect(links.length).toBeGreaterThan(0)
    const missing = links.filter((link) => {
      if (link.startsWith('#')) return !anchors.has(decodeURIComponent(link.slice(1)))
      if (/^[a-z][a-z0-9+.-]*:/i.test(link) || link.startsWith('//')) return false
      const target = decodeURIComponent(link.split(/[?#]/, 1)[0])
      return target !== '' && !existsSync(path.join(repositoryRoot, target))
    })
    expect(missing).toEqual([])
  })

  it('recognizes HTML anchors while ignoring navigation examples in code and comments', () => {
    const markdown = [
      '# Real heading',
      '<a name="explicit"></a>',
      '',
      '[Go](#explicit)',
      '`[Inline example](#inline-only)`',
      '',
      '```html',
      '<a id="fenced-only" href="#not-a-link"></a>',
      '```',
      '',
      '<!-- <a id="comment-only" href="#not-a-comment-link"></a> -->',
    ].join('\n')
    const { anchors, links } = readmeNavigation(markdown)
    expect([...anchors]).toEqual(['real-heading', 'explicit'])
    expect(links).toEqual(['#explicit'])
  })

  it('selects the exact architecture fence instead of another document diagram', () => {
    const expected = '````mermaid\nflowchart TB\n  A["Gateway"] --> B["API"]\n````'
    const markdown = [
      '```mermaid\nflowchart LR\n Wrong --> Diagram\n```',
      '## Architecture &amp; components',
      expected,
      '## Another section',
      '```mermaid\nflowchart LR\n Another --> Diagram\n```',
    ].join('\n\n')
    expect(readmeArchitectureMarkdown(markdown)).toBe(expected)
  })

  it('makes the documented gateway, modes and durable store available to the visual fixture', () => {
    const diagram = readmeArchitectureMarkdown(readme)
    expect(diagram).toContain('Python web gateway')
    expect(diagram).toContain('PostgreSQL')
    expect(diagram).toContain('Chat, Research, Knowledge and Agents')
  })
})
