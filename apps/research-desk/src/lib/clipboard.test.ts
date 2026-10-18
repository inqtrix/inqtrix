import { afterEach, describe, expect, it, vi } from 'vitest'

import { copyTextToClipboard } from './clipboard'

function stubClipboard(value: unknown): void {
  Object.defineProperty(navigator, 'clipboard', {
    configurable: true,
    value,
  })
}

class _FakeTextArea {
  value = ''
  removed = false
  private attributes: Record<string, string> = {}
  style: Record<string, string> = {}

  setAttribute(name: string, value: string): void {
    this.attributes[name] = value
  }

  select(): void {}

  setSelectionRange(): void {}

  remove(): void {
    this.removed = true
  }
}

function stubDocument(execResult: boolean | (() => boolean)) {
  const created: _FakeTextArea[] = []
  const exec = vi.fn(() =>
    typeof execResult === 'function' ? execResult() : execResult,
  )
  const fake = {
    createElement: () => {
      const node = new _FakeTextArea()
      created.push(node)
      return node
    },
    body: { appendChild: () => {} },
    activeElement: null,
    execCommand: exec,
  }
  ;(globalThis as Record<string, unknown>).document = fake
  return { created, exec }
}

afterEach(() => {
  stubClipboard(undefined)
  delete (globalThis as Record<string, unknown>).document
  vi.restoreAllMocks()
})

describe('copyTextToClipboard', () => {
  it('uses the async clipboard API when it exists', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    stubClipboard({ writeText })

    await expect(copyTextToClipboard('Inhalt')).resolves.toBe(true)
    expect(writeText).toHaveBeenCalledWith('Inhalt')
  })

  it('falls back to execCommand on insecure origins without the API', async () => {
    stubClipboard(undefined)
    const { created, exec } = stubDocument(true)

    await expect(copyTextToClipboard('LAN-Inhalt')).resolves.toBe(true)
    expect(exec).toHaveBeenCalledWith('copy')
    expect(created).toHaveLength(1)
    expect(created[0].value).toBe('LAN-Inhalt')
    expect(created[0].removed).toBe(true)
  })

  it('falls back when the async API throws (permission refused)', async () => {
    stubClipboard({
      writeText: vi.fn().mockRejectedValue(new Error('denied')),
    })
    const { exec } = stubDocument(true)

    await expect(copyTextToClipboard('Inhalt')).resolves.toBe(true)
    expect(exec).toHaveBeenCalledWith('copy')
  })

  it('reports an honest failure when every path is unavailable', async () => {
    stubClipboard(undefined)
    const { created } = stubDocument(false)

    await expect(copyTextToClipboard('Inhalt')).resolves.toBe(false)
    expect(created[0].removed).toBe(true)
  })
})
