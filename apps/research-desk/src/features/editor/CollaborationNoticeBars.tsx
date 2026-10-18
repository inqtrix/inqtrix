import { useEffect, useState } from 'react'
import type { Editor } from '@tiptap/react'

import { AlertTriangle, Download, Info, RefreshCw } from '@/components/icons'
import { Button } from '@/components/ui/button'
import { downloadMarkdownBlockBlob } from '@/components/markdown/markdownBlockExport'
import type { EditorDocumentRecord } from '@/features/project/types'
import { fileNameTimestamp } from './export/fileNameTimestamp'
import {
  bindInspectorSuggestionEvents,
  editRefusalFromEvent,
} from './inspector/collaborationAdapter'
import type { CollaborationEditRefusal } from './tiptap'
import { serializeEditorMarkdown } from './tiptap'

const COPY = {
  de: {
    backupFileSuffix: 'Sicherung',
    downloadBackup: 'Sicherung herunterladen',
    openRecoveryCopy: 'Kopie öffnen',
    recoveryCopyKept: 'Ihr nicht gespeicherter Stand wurde als Wiederherstellungskopie abgelegt.',
    reload: 'Neu laden',
  },
  en: {
    backupFileSuffix: 'backup',
    downloadBackup: 'Download backup',
    openRecoveryCopy: 'Open copy',
    recoveryCopyKept: 'Your unsaved state was kept as a recovery copy.',
    reload: 'Reload',
  },
} as const

/** The editor's last refusal of a local change, until a change goes through
 * or another editor is bound. */
export function useCollaborationEditRefusal(
  editor: Editor | null,
): CollaborationEditRefusal | null {
  const [refusal, setRefusal] = useState<CollaborationEditRefusal | null>(null)
  useEffect(() => {
    setRefusal(null)
    if (!editor) return
    return bindInspectorSuggestionEvents(editor, {
      onCollision: () => undefined,
      onError: (event) => setRefusal(editRefusalFromEvent(event)),
    }) ?? undefined
  }, [editor])
  return refusal
}

/** Why the editor refused the last local change. It stays until a change
 * goes through, so the reason is readable after the keystroke; it is a
 * polite status, never an alert, because it answers the user's own action. */
export function CollaborationEditRefusalLine({ message }: { message: string }) {
  return (
    <div
      className="flex min-h-9 items-center gap-2 border-b border-border bg-muted/40 px-3 py-1"
      data-collaboration-edit-refusal
      role="status"
    >
      <Info aria-hidden className="icon-sm shrink-0 text-muted-foreground" />
      <p className="t-meta min-w-0 flex-1 text-foreground">{message}</p>
    </div>
  )
}

/** The newest recovery copy that keeps the local state this document held
 * when its collaboration generation was replaced, or null. */
export function replacedGenerationRecoveryCopy(
  documents: Readonly<Record<string, EditorDocumentRecord>>,
  documentId: string,
): EditorDocumentRecord | null {
  let newest: { capturedAt: string; document: EditorDocumentRecord } | null = null
  for (const document of Object.values(documents)) {
    const recovery = document.recovery
    if (
      recovery?.reason !== 'generation_replaced'
      || recovery.originalDocumentId !== documentId
    ) continue
    if (newest === null || recovery.capturedAt > newest.capturedAt) {
      newest = { capturedAt: recovery.capturedAt, document }
    }
  }
  return newest?.document ?? null
}

/** The document's generation was replaced while the service could not
 * confirm its local state, which now waits as a recovery copy beside it.
 * The notice stays while the copy exists, so the unsaved state is never
 * out of sight; the link opens the copy. */
export function CollaborationRecoveryCopyNotice({
  locale,
  onOpenCopy,
}: {
  locale: 'de' | 'en'
  onOpenCopy: () => void
}) {
  const labels = COPY[locale]
  return (
    <div
      className="flex min-h-9 flex-wrap items-center gap-x-2 border-b border-border bg-muted/40 px-3 py-1"
      data-collaboration-recovery-copy
      role="status"
    >
      <Info aria-hidden className="icon-sm shrink-0 text-muted-foreground" />
      <p className="t-meta min-w-0 flex-1 text-foreground">{labels.recoveryCopyKept}</p>
      <Button className="h-7 px-1" onClick={onOpenCopy} size="sm" type="button" variant="link">
        {labels.openRecoveryCopy}
      </Button>
    </div>
  )
}

/** The service refused a local update: the editor shows the local state,
 * read-only, as a backup. Reloading starts the document over from the
 * service's state; the backup can be saved first. */
export function CollaborationRejectionBar({
  locale,
  notice,
  onDownloadBackup,
  onReload,
}: {
  locale: 'de' | 'en'
  notice: string
  onDownloadBackup: (() => void) | null
  onReload: () => void
}) {
  const labels = COPY[locale]
  return (
    <div
      className="flex flex-wrap items-center gap-2 border-b border-warning/40 bg-warning-subtle px-3 py-2"
      data-collaboration-update-rejected
      role="alert"
    >
      <AlertTriangle aria-hidden className="icon-sm shrink-0 text-warning" />
      <p className="t-meta min-w-64 flex-1 text-foreground">{notice}</p>
      {onDownloadBackup ? (
        <Button onClick={onDownloadBackup} size="sm" type="button" variant="ghost">
          <Download aria-hidden />
          {labels.downloadBackup}
        </Button>
      ) : null}
      <Button onClick={onReload} size="sm" type="button" variant="outline">
        <RefreshCw aria-hidden />
        {labels.reload}
      </Button>
    </div>
  )
}

/** The file a backup of the local document state is saved as. */
export function collaborationBackupFile(
  markdown: string,
  title: string,
  locale: 'de' | 'en',
  now: Date,
): { contents: string; fileName: string } {
  // Characters file systems refuse become spaces, and so do control and
  // invisible format characters: a direction override would show the name
  // with another extension than the file has.
  const base = title
    .replace(/[\\/:*?"<>|]|\p{Cc}|\p{Cf}/gu, ' ')
    .replace(/\s+/g, ' ')
    .trim()
  return {
    contents: markdown,
    fileName: `${base || 'Inqtrix'} - ${COPY[locale].backupFileSuffix} ${fileNameTimestamp(now)}.md`,
  }
}

/** Saves the editor's local state as a Markdown file. */
export function downloadCollaborationBackup(
  editor: Editor,
  title: string,
  locale: 'de' | 'en',
): void {
  const file = collaborationBackupFile(serializeEditorMarkdown(editor), title, locale, new Date())
  downloadMarkdownBlockBlob(
    new Blob([file.contents], { type: 'text/markdown;charset=utf-8' }),
    file.fileName,
  )
}
