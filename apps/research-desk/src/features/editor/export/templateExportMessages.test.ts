import { describe, expect, it } from 'vitest'
import { editorCopy } from '../editorCopy'
import {
  TemplateExportError,
  type TemplateExportErrorCode,
  templateExportErrorText,
  templateExportNoticeText,
} from './templateExportMessages'

const ERROR_CODES: TemplateExportErrorCode[] = [
  'content_marker_not_alone',
  'content_marker_outside_body',
  'content_marker_repeated',
  'template_has_macros',
  'template_is_strict',
  'template_too_large',
  'template_unreadable',
]

describe('templateExportErrorText', () => {
  it('gives every refusal its own text in both languages', () => {
    for (const copy of [editorCopy.de, editorCopy.en]) {
      const texts = ERROR_CODES.map((code) => templateExportErrorText(new TemplateExportError(code), copy))
      expect(new Set(texts).size).toBe(ERROR_CODES.length)
      expect(texts).not.toContain(copy.exportWordTemplateFailed)
    }
  })

  it('does not pass a technical error on to the user', () => {
    const text = templateExportErrorText(new Error('secret internal detail'), editorCopy.en)
    expect(text).toBe(editorCopy.en.exportWordTemplateFailed)
  })
})

describe('templateExportNoticeText', () => {
  it('names the heading levels and the markers it is about', () => {
    const copy = editorCopy.de
    expect(templateExportNoticeText({ code: 'heading_levels_borrowed', levels: [3, 4] }, copy)).toContain('Überschrift 3, 4')
    expect(templateExportNoticeText({ code: 'markers_not_replaced', markers: ['{{date}}', '{{title}}'] }, copy))
      .toContain('{{date}}, {{title}} wurde')
  })

  it('names the marker the user can add', () => {
    for (const copy of [editorCopy.de, editorCopy.en]) {
      expect(templateExportNoticeText({ code: 'content_marker_missing' }, copy)).toContain('{{content}}')
    }
  })
})
