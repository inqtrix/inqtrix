import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { setRequestErrorLocale } from '@/api/requestErrorText'

// Mermaid needs a REAL laid-out node to measure a diagram. Given no container
// it appends that node to document.body — and a body child below the fold
// gives the DOCUMENT scroll extent (html/body are overflow: visible). In this
// fixed-shell app that painted a window scrollbar for the duration of the
// render and shifted the whole UI sideways by the scrollbar width; a wheel
// over any surface without an inner scroller — the left app rail — also
// scrolled the shell. ensureMermaidRender therefore hands mermaid its own
// out-of-flow host, and still removes the `#d<renderId>` node mermaid LEAKS
// on a parse error. Both halves of that contract are what this file pins.
//
// The suite runs in the node environment on purpose (repo convention), so the
// test stubs exactly the DOM surface the contract touches. Layout itself
// cannot be asserted here — that `document.scrollHeight` stays flat during a
// render is browser verification, not a node-env unit test.

type StubElement = {
  id: string
  style: { cssText: string }
  attributes: Record<string, string>
  children: StubElement[]
  parent: StubElement | null
  setAttribute: (name: string, value: string) => void
  appendChild: (child: StubElement) => void
  remove: () => void
}

type DocumentStub = {
  body: StubElement
  createElement: () => StubElement
  documentElement: Record<string, never>
  getElementById: (id: string) => StubElement | null
}

function makeElement(): StubElement {
  const el: StubElement = {
    id: '',
    style: { cssText: '' },
    attributes: {},
    children: [],
    parent: null,
    setAttribute: (name, value) => { el.attributes[name] = value },
    appendChild: (child) => {
      child.parent?.children.splice(child.parent.children.indexOf(child), 1)
      child.parent = el
      el.children.push(child)
    },
    remove: () => {
      const siblings = el.parent?.children
      if (siblings) siblings.splice(siblings.indexOf(el), 1)
      el.parent = null
    },
  }
  return el
}

function findById(root: StubElement, id: string): StubElement | null {
  for (const child of root.children) {
    if (child.id === id) return child
    const found = findById(child, id)
    if (found) return found
  }
  return null
}

function installDomStub(): StubElement {
  const body = makeElement()
  const documentStub: DocumentStub = {
    body,
    createElement: () => makeElement(),
    documentElement: {},
    getElementById: (id) => findById(body, id),
  }
  vi.stubGlobal('document', documentStub)
  vi.stubGlobal('getComputedStyle', () => ({ getPropertyValue: () => '' }))
  return body
}

/** What mermaid was handed as its third argument, per call. */
const containerArgs: Array<StubElement | undefined> = []
/** Every config object handed to mermaid.initialize, per call. */
const initializeArgs: Array<Record<string, unknown>> = []
/** The diagram source mermaid was asked to render, per call. */
const renderTexts: string[] = []
/** Marker that switches the render mock to the SUCCESS path. */
const RENDER_OK_MARKER = 'SANITIZEPROBE'
const MOCK_RENDER_SVG = '<svg><text>ok</text></svg>'

vi.mock('mermaid', () => ({
  default: {
    initialize: (config: Record<string, unknown>) => {
      initializeArgs.push(config)
    },
    render: (id: string, text: string, container?: StubElement) => {
      containerArgs.push(container)
      renderTexts.push(text)
      const doc = globalThis.document as unknown as DocumentStub
      // Mermaid builds `#d<id>` inside the container it was given, and falls
      // back to document.body when it was given none.
      const stray = doc.createElement()
      stray.id = `d${id}`
      ;(container ?? doc.body).appendChild(stray)
      if (text.includes(RENDER_OK_MARKER)) {
        return Promise.resolve({ svg: MOCK_RENDER_SVG })
      }
      throw new Error('No diagram type detected')
    },
  },
}))

let body: StubElement

beforeEach(() => {
  containerArgs.length = 0
  initializeArgs.length = 0
  renderTexts.length = 0
  body = installDomStub()
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('mermaid measurement host', () => {
  it('renders into an out-of-flow host instead of document.body', async () => {
    const { ensureMermaidRender } = await import('./MermaidFigure')
    await ensureMermaidRender('graph TD; A-->B', 'light', 'standard', 'standard')

    const host = body.children.find((el) => el.id === 'inqtrix-mermaid-measure')
    expect(host).toBeDefined()
    // Mermaid must have been handed that host, never left to default to body.
    expect(containerArgs).toEqual([host])
    // `fixed` is the whole point: a laid-out node that cannot give the
    // document scroll extent. `hidden` (not `display: none`) keeps the boxes
    // mermaid measures with getBBox.
    expect(host?.style.cssText).toContain('position:fixed')
    expect(host?.style.cssText).toContain('visibility:hidden')
    expect(host?.attributes['aria-hidden']).toBe('true')
  })

  it('reuses one host across renders', async () => {
    const { ensureMermaidRender } = await import('./MermaidFigure')
    await ensureMermaidRender('erste', 'light', 'standard', 'standard')
    await ensureMermaidRender('zweite', 'light', 'standard', 'standard')

    const hosts = body.children.filter((el) => el.id === 'inqtrix-mermaid-measure')
    expect(hosts).toHaveLength(1)
    expect(containerArgs).toHaveLength(2)
  })

  it('removes the leaked render container when the diagram fails to parse', async () => {
    const { ensureMermaidRender } = await import('./MermaidFigure')
    await ensureMermaidRender('kaputt', 'light', 'standard', 'standard')

    // The error itself stays loud (the figure renders the message + source);
    // only the invisible DOM side effect must be gone — the host is left
    // empty, and no stray is ever a direct body child.
    const host = body.children.find((el) => el.id === 'inqtrix-mermaid-measure')
    expect(host?.children).toEqual([])
    expect(body.children.some((el) => el.id.startsWith('dinqtrix-mermaid'))).toBe(false)
  })
})

describe('mermaid math label security', () => {
  it('keeps the SVG-text path (no HTML labels, no purify config) without math', async () => {
    const { ensureMermaidRender } = await import('./MermaidFigure')
    await ensureMermaidRender('graph TD; O-->P', 'light', 'standard', 'standard')

    const config = initializeArgs.at(-1)
    expect(config).toBeDefined()
    expect(config?.htmlLabels).toBe(false)
    expect(config?.securityLevel).toBe('strict')
    expect(config && 'dompurifyConfig' in config).toBe(false)
  })

  it('switches a math source to HTML labels with the pinned sanitize policy', async () => {
    const { ensureMermaidRender } = await import('./MermaidFigure')
    await ensureMermaidRender('graph TD; M["$$E=mc^2$$"]-->N', 'light', 'standard', 'standard')

    const config = initializeArgs.at(-1)
    expect(config?.htmlLabels).toBe(true)
    expect(config?.securityLevel).toBe('strict')
    const purify = config?.dompurifyConfig as Record<string, string[]> | undefined
    expect(purify?.FORBID_TAGS).toEqual(expect.arrayContaining([
      'style', 'img', 'image', 'video', 'audio', 'source', 'track', 'iframe', 'object', 'embed',
    ]))
    expect(purify?.FORBID_ATTR).toEqual(expect.arrayContaining([
      'src', 'srcset', 'poster', 'background', 'ping',
    ]))
    expect(purify?.ADD_TAGS).toEqual(expect.arrayContaining([
      'foreignobject', 'semantics', 'annotation', 'annotation-xml',
    ]))
  })

  it('stores a successful non-math render byte-identical (no sanitizer pass)', async () => {
    const { ensureMermaidRender, peekMermaidRender } = await import('./MermaidFigure')
    const source = `graph TD; ${RENDER_OK_MARKER}-->Q`
    await ensureMermaidRender(source, 'light', 'standard', 'standard')

    expect(peekMermaidRender(source, 'light', 'standard', 'standard')).toEqual({
      directivesStripped: false,
      kind: 'svg',
      svg: MOCK_RENDER_SVG,
    })
  })

  it('fails CLOSED into a visible error when the sanitizer is unavailable', async () => {
    const { ensureMermaidRender, peekMermaidRender } = await import('./MermaidFigure')
    // The error reads in the reader's language; this test states German.
    setRequestErrorLocale('de')
    // Node has no DOM, so DOMPurify reports unsupported — the math render
    // must become a loud error entry, never an unsanitized svg entry.
    const source = `graph TD; S["$$x$$ ${RENDER_OK_MARKER}"]-->T`
    await ensureMermaidRender(source, 'light', 'standard', 'standard')
    setRequestErrorLocale('en')

    const entry = peekMermaidRender(source, 'light', 'standard', 'standard')
    expect(entry?.kind).toBe('error')
    expect(entry && entry.kind === 'error' ? entry.message : '').toContain('SVG-Sanitizer')
  })
})

describe('mermaid theme and bounds', () => {
  it('hands mermaid the app theme: transparent ground, token ink, one type size', async () => {
    const { ensureMermaidRender } = await import('./MermaidFigure')
    await ensureMermaidRender('graph TD; T-->U', 'dark', 'standard', 'standard')
    const chat = initializeArgs.at(-1)
    expect(chat?.theme).toBe('base')
    const variables = chat?.themeVariables as Record<string, unknown>
    expect(variables.background).toBe('transparent')
    expect(variables.darkMode).toBe(true)
    expect(variables.fontSize).toBe('13px')
    // No token in this stub environment: the per-theme fallbacks stay
    // consistent — the node fill is the surface, never mermaid's default.
    expect(variables.primaryColor).toBe('#1b1e27')
    expect((variables.xyChart as Record<string, unknown>).backgroundColor).toBe('transparent')
    const xyChart = chat?.xyChart as Record<string, unknown>
    expect(xyChart).toMatchObject({ height: 330, useMaxWidth: true, width: 440 })
    expect((chat?.sequence as Record<string, unknown>).diagramMarginX).toBe(24)
    expect((chat?.gantt as Record<string, unknown>).fontSize).toBe(13)
    expect(chat?.fontSize).toBe(13)
    // The same source on the other surface is one shared render.
    await ensureMermaidRender('graph TD; T-->U', 'dark', 'standard', 'standard')
    expect(initializeArgs).toHaveLength(1)
  })

  it('draws an adaptive chart at the measured width as its own cache entry, type unchanged', async () => {
    const { ensureMermaidRender, peekMermaidRender } = await import('./MermaidFigure')
    const source = `xychart-beta\n  x-axis [a, b]\n  bar [1, 2] ${RENDER_OK_MARKER}`
    await ensureMermaidRender(source, 'light', 'standard', 'standard', 320)
    const narrow = initializeArgs.at(-1)
    expect(narrow?.xyChart).toMatchObject({ height: 240, width: 320 })
    expect((narrow?.themeVariables as Record<string, unknown>).fontSize).toBe('13px')
    await ensureMermaidRender(source, 'light', 'standard', 'standard', 440)
    expect((initializeArgs.at(-1)?.xyChart as Record<string, unknown>).width).toBe(440)
    expect(peekMermaidRender(source, 'light', 'standard', 'standard', 320)?.kind).toBe('svg')
    expect(peekMermaidRender(source, 'light', 'standard', 'standard', 440)?.kind).toBe('svg')
    expect(peekMermaidRender(source, 'light', 'standard', 'standard')).toBeUndefined()
  })

  it('hands mermaid a stored diagram with its keyword first, without a stripped notice', async () => {
    const { ensureMermaidRender, peekMermaidRender } = await import('./MermaidFigure')
    const source = `accTitle: Ablauf\nflowchart TD\n  A["${RENDER_OK_MARKER}"] --> B`
    await ensureMermaidRender(source, 'light', 'standard', 'standard')

    expect(renderTexts.at(-1)).toBe(`flowchart TD\naccTitle: Ablauf\n  A["${RENDER_OK_MARKER}"] --> B`)
    expect(peekMermaidRender(source, 'light', 'standard', 'standard')).toEqual({
      directivesStripped: false,
      kind: 'svg',
      svg: MOCK_RENDER_SVG,
    })
  })

  it('strips styling directives before mermaid sees the source and remembers it for the figure', async () => {
    const { ensureMermaidRender, peekMermaidRender } = await import('./MermaidFigure')
    const source = `%%{init: {"theme": "forest"}}%%\ngraph TD; ${RENDER_OK_MARKER}-->V`
    await ensureMermaidRender(source, 'light', 'standard', 'standard')

    expect(renderTexts.at(-1)).toBe(`graph TD; ${RENDER_OK_MARKER}-->V`)
    expect(peekMermaidRender(source, 'light', 'standard', 'standard')).toEqual({
      directivesStripped: true,
      kind: 'svg',
      svg: MOCK_RENDER_SVG,
    })
  })
})
