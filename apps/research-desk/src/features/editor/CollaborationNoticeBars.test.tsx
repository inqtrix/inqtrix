import { renderToStaticMarkup } from 'react-dom/server'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { EditorDocumentRecord } from '@/features/project/types'
import {
  CollaborationEditRefusalLine,
  CollaborationRecoveryCopyNotice,
  CollaborationRejectionBar,
  collaborationBackupFile,
  replacedGenerationRecoveryCopy,
} from './CollaborationNoticeBars'
import { COLLABORATION_NOTICES } from './collaborationNotices'
import { localizeEditorCollaborationNotice } from './inspector/model'

describe('the rejection bar', () => {
  const notice = localizeEditorCollaborationNotice(COLLABORATION_NOTICES.update_rejected, 'de')!

  it('alerts with the German notice and offers the backup and a reload', () => {
    const markup = renderToStaticMarkup(
      <CollaborationRejectionBar
        locale="de"
        notice={notice}
        onDownloadBackup={vi.fn()}
        onReload={vi.fn()}
      />,
    )

    expect(markup).toContain('role="alert"')
    expect(markup).toContain('Ihr lokaler Stand bleibt als Sicherung erhalten, bis Sie das Dokument neu laden.')
    expect(markup).toContain('>Sicherung herunterladen</button>')
    expect(markup).toContain('>Neu laden</button>')
  })

  it('still offers the reload when there is no editor to save a backup from', () => {
    const markup = renderToStaticMarkup(
      <CollaborationRejectionBar locale="en" notice="Rejected." onDownloadBackup={null} onReload={vi.fn()} />,
    )

    expect(markup).not.toContain('Download backup')
    expect(markup).toContain('>Reload</button>')
  })
})

describe('the recovery copy notice', () => {
  function document(
    id: string,
    recovery?: EditorDocumentRecord['recovery'],
  ): EditorDocumentRecord {
    return {
      contentMarkdown: `# ${id}`,
      createdAt: '2026-09-28T08:00:00.000Z',
      folderId: null,
      id,
      ...(recovery ? { recovery } : {}),
      revision: 1,
      source: 'blank',
      title: id,
      updatedAt: '2026-09-28T08:00:00.000Z',
    }
  }

  it('says in German that the unsaved state was kept as a recovery copy and offers to open it', () => {
    const markup = renderToStaticMarkup(
      <CollaborationRecoveryCopyNotice locale="de" onOpenCopy={vi.fn()} />,
    )

    expect(markup).toContain('role="status"')
    expect(markup).not.toContain('role="alert"')
    expect(markup).toContain('Ihr nicht gespeicherter Stand wurde als Wiederherstellungskopie abgelegt.')
    expect(markup).toMatch(/<button[^>]*>Kopie öffnen<\/button>/)
  })

  it('names the newest copy of a replaced generation of this document, never a copy of a deleted or of another document', () => {
    const documents = Object.fromEntries([
      document('original'),
      document('older-copy', {
        capturedAt: '2026-09-28T09:00:00.000Z',
        originalDocumentId: 'original',
        reason: 'generation_replaced',
      }),
      document('newer-copy', {
        capturedAt: '2026-09-28T10:00:00.000Z',
        originalDocumentId: 'original',
        reason: 'generation_replaced',
      }),
      document('deleted-copy', {
        capturedAt: '2026-09-28T11:00:00.000Z',
        originalDocumentId: 'original',
        reason: 'remote_deleted',
      }),
      document('foreign-copy', {
        capturedAt: '2026-09-28T12:00:00.000Z',
        originalDocumentId: 'other',
        reason: 'generation_replaced',
      }),
    ].map((entry) => [entry.id, entry]))

    expect(replacedGenerationRecoveryCopy(documents, 'original')?.id).toBe('newer-copy')
    expect(replacedGenerationRecoveryCopy(documents, 'other')?.id).toBe('foreign-copy')
    expect(replacedGenerationRecoveryCopy(documents, 'unrelated')).toBeNull()
    const withoutReplaced = Object.fromEntries(Object.entries(documents).filter(
      ([id]) => id !== 'older-copy' && id !== 'newer-copy',
    ))
    expect(replacedGenerationRecoveryCopy(withoutReplaced, 'original')).toBeNull()
  })
})

describe('the edit refusal line', () => {
  it('states the reason politely as a status', () => {
    const markup = renderToStaticMarkup(
      <CollaborationEditRefusalLine message="Im Modus „Vorschlagen“ kann ein neuer Absatz nur am Ende eines Absatzes beginnen." />,
    )

    expect(markup).toContain('role="status"')
    expect(markup).not.toContain('role="alert"')
    expect(markup).toContain('Im Modus „Vorschlagen“ kann ein neuer Absatz nur am Ende eines Absatzes beginnen.')
  })
})

describe('the backup file', () => {
  // The reader's clock names the file: 10:11:12 UTC is 12:11:12 in Berlin
  // in September. A zone other than UTC makes a UTC stamp visible here.
  beforeEach(() => {
    vi.stubEnv('TZ', 'Europe/Berlin')
  })
  afterEach(() => {
    vi.unstubAllEnvs()
  })
  const now = new Date('2026-09-27T10:11:12.345Z')

  it('keeps the local Markdown unchanged under a file name with the title and the local time', () => {
    expect(collaborationBackupFile('# Plan\n\nText', 'Plan Q3', 'de', now)).toEqual({
      contents: '# Plan\n\nText',
      fileName: 'Plan Q3 - Sicherung 20260927-121112.md',
    })
    expect(collaborationBackupFile('x', 'Plan Q3', 'en', now).fileName)
      .toBe('Plan Q3 - backup 20260927-121112.md')
  })

  it('replaces characters a file name cannot carry', () => {
    expect(collaborationBackupFile('x', 'Plan: Q3/Q4 <draft>\u0007', 'de', now).fileName)
      .toBe('Plan Q3 Q4 draft - Sicherung 20260927-121112.md')
    expect(collaborationBackupFile('x', '  ', 'de', now).fileName)
      .toBe('Inqtrix - Sicherung 20260927-121112.md')
  })

  it('drops invisible direction and control characters that would disguise the file name', () => {
    // A right-to-left override shows "Plan\u202Egpj.md" as "Plandm.jpg".
    expect(collaborationBackupFile('x', 'Plan\u202Egpj\u200F\u007F\u0085', 'de', now).fileName)
      .toBe('Plan gpj - Sicherung 20260927-121112.md')
  })
})
