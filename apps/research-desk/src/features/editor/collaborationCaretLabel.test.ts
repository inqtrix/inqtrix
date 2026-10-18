import type { Plugin } from '@tiptap/pm/state'
import type { EditorView } from '@tiptap/pm/view'
import { getExtensionField } from '@tiptap/core'
import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  CollaborationReviewExtension,
  collaborationCaretLabelColor,
  collaborationReviewPluginKey,
  renderCollaborationCaret,
} from './tiptap'

/** An inline style that serializes like a browser: colors read back as
 * `rgb(...)`, and writing the value a property already has changes nothing,
 * so it counts no mutation. */
class FakeStyle {
  private readonly values = new Map<string, { priority: string; value: string }>()
  mutations = 0

  getPropertyValue(property: string): string {
    return this.values.get(property)?.value ?? ''
  }

  getPropertyPriority(property: string): string {
    return this.values.get(property)?.priority ?? ''
  }

  setProperty(property: string, value: string, priority = ''): void {
    const serialized = value.replace(/#([0-9a-f]{6})\b/gi, (_match, hex: string) => (
      `rgb(${Number.parseInt(hex.slice(0, 2), 16)}, ${Number.parseInt(hex.slice(2, 4), 16)}, ${Number.parseInt(hex.slice(4, 6), 16)})`
    ))
    const current = this.values.get(property)
    if (current?.value === serialized && current.priority === priority) return
    this.values.set(property, { priority, value: serialized })
    this.mutations += 1
  }

  /** Only a `border-color` declaration sets it, not `border-left`. */
  get borderColor(): string {
    return this.getPropertyValue('border-color')
  }

  get backgroundColor(): string {
    return this.getPropertyValue('background-color')
  }
}

type Rect = { left: number; right: number; width: number }

class FakeElement {
  readonly children: FakeElement[] = []
  readonly classes = new Set<string>()
  readonly dataset: Record<string, string> = {}
  readonly style = new FakeStyle()
  readonly listeners = new Map<string, Array<{ capture: boolean; listener: () => void }>>()
  attributes = new Map<string, string>()
  className = ''
  textContent = ''
  rect: Rect = { left: 0, right: 0, width: 0 }
  ownerDocument = { defaultView: { innerWidth: 1_200 } }

  readonly classList = {
    add: (...names: string[]) => {
      for (const name of names) this.classes.add(name)
    },
  }

  append(child: FakeElement): void {
    this.children.push(child)
  }

  setAttribute(name: string, value: string): void {
    this.attributes.set(name, value)
  }

  getBoundingClientRect(): Rect {
    return this.rect
  }

  querySelector(): FakeElement | null {
    return this.children[0] ?? null
  }

  querySelectorAll(selector: string): FakeElement[] {
    if (selector.includes('selection')) return []
    return this.children
  }

  addEventListener(
    type: string,
    listener: () => void,
    options?: boolean | { capture?: boolean },
  ): void {
    const capture = typeof options === 'boolean' ? options : options?.capture === true
    this.listeners.set(type, [...(this.listeners.get(type) ?? []), { capture, listener }])
  }

  removeEventListener(type: string, listener: () => void): void {
    this.listeners.set(
      type,
      (this.listeners.get(type) ?? []).filter((entry) => entry.listener !== listener),
    )
  }

  mutations(): number {
    return this.style.mutations + this.children.reduce((sum, child) => sum + child.mutations(), 0)
  }
}

function stubDocument() {
  vi.stubGlobal('document', {
    createElement: () => new FakeElement(),
  })
}

/** The review plugin's view over a root holding one rendered caret, with
 * frames and style observation run by the test. */
function presenceLayout(color: string) {
  stubDocument()
  const frames = new Map<number, () => void>()
  let nextFrame = 1
  let observe: (() => void) | null = null
  vi.stubGlobal('requestAnimationFrame', (callback: () => void) => {
    const id = nextFrame
    nextFrame += 1
    frames.set(id, callback)
    return id
  })
  vi.stubGlobal('cancelAnimationFrame', (id: number) => {
    frames.delete(id)
  })
  vi.stubGlobal('MutationObserver', class {
    constructor(callback: () => void) {
      observe = callback
    }
    disconnect() {}
    observe() {}
  })
  vi.stubGlobal('ResizeObserver', class {
    disconnect() {}
    observe() {}
  })
  vi.stubGlobal('window', {
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
  })

  const root = new FakeElement()
  root.rect = { left: 100, right: 700, width: 600 }
  const caret = renderCollaborationCaret({ color, name: 'Ada Lovelace' }) as unknown as FakeElement
  caret.rect = { left: 400, right: 402, width: 2 }
  const label = caret.children[0]!
  label.rect = { left: 400, right: 480, width: 80 }
  root.append(caret)

  const extension = CollaborationReviewExtension.configure({})
  const storage = getExtensionField<() => Record<string, unknown>>(
    extension,
    'addStorage',
    { name: extension.name, options: extension.options },
  )()
  const plugins = getExtensionField<() => Plugin[]>(extension, 'addProseMirrorPlugins', {
    editor: {} as never,
    name: extension.name,
    options: extension.options,
    storage,
    type: null,
  })()
  const plugin = plugins.find((candidate) => candidate.spec.key === collaborationReviewPluginKey)!
  const view = { dom: root } as unknown as EditorView
  const pluginView = plugin.spec.view!(view)

  /** Runs the scheduled frames; a frame that changed a style is observed,
   * as a MutationObserver would, and may schedule the next. */
  const runFrame = (): number => {
    const pending = [...frames.entries()]
    frames.clear()
    const before = root.mutations()
    for (const [, callback] of pending) callback()
    const written = root.mutations() - before
    if (written > 0) observe?.()
    return written
  }
  return { caret, frames, label, pluginView, root, runFrame }
}

describe('collaboration caret label layout', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('writes no style while a caret label rests', () => {
    const layout = presenceLayout('#2563EB')
    // The first frames settle the layout.
    for (let frame = 0; frame < 3; frame += 1) layout.runFrame()

    const written = [layout.runFrame(), layout.runFrame(), layout.runFrame()]

    expect(written).toEqual([0, 0, 0])
    expect(layout.frames.size).toBe(0)
    layout.pluginView.destroy?.()
  })

  it('keeps dark label text on a bright user colour after every layout', () => {
    const layout = presenceLayout('#FACC15')
    expect(layout.label.style.getPropertyValue('color')).toBe('rgb(17, 24, 39)')

    for (let frame = 0; frame < 3; frame += 1) layout.runFrame()

    expect(layout.label.style.getPropertyValue('color')).toBe('rgb(17, 24, 39)')
    layout.pluginView.destroy?.()
  })

  it('lays the label out again when a container inside the editor scrolls sideways', () => {
    const layout = presenceLayout('#2563EB')
    for (let frame = 0; frame < 3; frame += 1) layout.runFrame()
    const scroll = (layout.root.listeners.get('scroll') ?? []).filter((entry) => entry.capture)
    expect(scroll).toHaveLength(1)

    // The table wrapper scrolled: the caret now sits near the left edge.
    layout.caret.rect = { left: 90, right: 92, width: 2 }
    layout.label.rect = { left: 90, right: 170, width: 80 }
    scroll[0]!.listener()
    expect(layout.frames.size).toBe(1)
    layout.runFrame()
    expect(layout.label.style.getPropertyValue('transform')).toBe('translateX(10px)')

    layout.pluginView.destroy?.()
    expect((layout.root.listeners.get('scroll') ?? [])).toHaveLength(0)
  })
})

describe('collaborationCaretLabelColor', () => {
  it('picks dark text for a bright user colour and white text otherwise', () => {
    expect(collaborationCaretLabelColor('#FACC15')).toBe('#111827')
    expect(collaborationCaretLabelColor('#2563EB')).toBe('#ffffff')
    expect(collaborationCaretLabelColor('var(--brand)')).toBe('#ffffff')
  })
})
