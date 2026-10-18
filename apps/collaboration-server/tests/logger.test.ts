import { serializeEditorJson, setCitationLabelsLeftEscapedReporter } from '@inqtrix/editor-schema'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { createJsonLogger, routeEditorSchemaWarnings } from '../src/logger'

afterEach(() => {
  setCitationLabelsLeftEscapedReporter(null)
  vi.restoreAllMocks()
})

describe('editor schema warnings', () => {
  it('reach the sidecar log as structured records, not as console text', () => {
    const consoleWarn = vi.spyOn(console, 'warn').mockImplementation(() => undefined)
    const sink = { debug: vi.fn(), error: vi.fn(), info: vi.fn(), warn: vi.fn() }
    routeEditorSchemaWarnings(createJsonLogger(sink))

    const projection = serializeEditorJson({
      type: 'doc',
      content: [{ type: 'paragraph', content: [{ type: 'text', text: 'Satz [K1] ﷐' }] }],
    })

    expect(projection).toBe('Satz \\[K1\\] ﷐')
    expect(consoleWarn).not.toHaveBeenCalled()
    expect(sink.warn).toHaveBeenCalledTimes(1)
    expect(JSON.parse(String(sink.warn.mock.calls[0]?.[0]))).toMatchObject({
      component: 'collaboration-server',
      event: 'citation_labels_left_escaped',
      level: 'warn',
      reason: 'placeholder_character_in_document',
    })
  })
})
