/**
 * What an export into a Word template can tell the user. Apart from the export
 * itself, so the editor can word a failure without loading the export's chunk.
 */
import type { EditorCopy } from '../editorCopy'

export type TemplateExportErrorCode =
  | 'content_marker_not_alone'
  | 'content_marker_outside_body'
  | 'content_marker_repeated'
  | 'template_has_macros'
  | 'template_is_strict'
  | 'template_too_large'
  | 'template_unreadable'

export type TemplateExportNotice =
  | { code: 'content_marker_missing' }
  | { code: 'heading_levels_borrowed'; levels: number[] }
  | { code: 'heading_styles_missing' }
  | { code: 'markers_not_replaced'; markers: string[] }

/** The template cannot take the report, for a reason the user can act on. */
export class TemplateExportError extends Error {
  readonly code: TemplateExportErrorCode

  constructor(code: TemplateExportErrorCode) {
    super(`Word template refused: ${code}`)
    this.name = 'TemplateExportError'
    this.code = code
  }
}

const ERROR_COPY: Record<TemplateExportErrorCode, keyof EditorCopy> = {
  content_marker_not_alone: 'exportTemplateMarkerNotAlone',
  content_marker_outside_body: 'exportTemplateMarkerOutsideBody',
  content_marker_repeated: 'exportTemplateMarkerRepeated',
  template_has_macros: 'exportTemplateHasMacros',
  template_is_strict: 'exportTemplateIsStrict',
  template_too_large: 'exportTemplateTooLarge',
  template_unreadable: 'exportTemplateUnreadable',
}

export function templateExportErrorText(error: unknown, copy: EditorCopy): string {
  return error instanceof TemplateExportError
    ? copy[ERROR_COPY[error.code]]
    : copy.exportWordTemplateFailed
}

export function templateExportNoticeText(notice: TemplateExportNotice, copy: EditorCopy): string {
  switch (notice.code) {
    case 'content_marker_missing':
      return copy.exportTemplateMarkerMissing
    case 'heading_levels_borrowed':
      return copy.exportTemplateHeadingsBorrowed.replace('{levels}', () => notice.levels.join(', '))
    case 'heading_styles_missing':
      return copy.exportTemplateHeadingsMissing
    case 'markers_not_replaced':
      return copy.exportTemplateMarkersKept.replace('{markers}', () => notice.markers.join(', '))
  }
}
