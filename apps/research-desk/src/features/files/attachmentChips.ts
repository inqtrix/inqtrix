import { BookOpen, FileText, FolderOpen, Paperclip, type LucideIcon } from '@/components/icons'
import type { ChatContextReferenceRecord } from '@/features/project/types'

export type AttachmentChipVisual = {
  chipClassName: string
  icon: LucideIcon
}

/**
 * Visual treatment per attachment kind. Single source of truth so the composer
 * draft chips and the sent-message chips never drift apart. File attachments use
 * the dedicated `--file` token (teal), kept visually distinct from research
 * reports (brand) and chat rules (success); single files and groups share the
 * token and differ only by icon (paperclip vs. open folder).
 */
export function attachmentChipVisual(kind: ChatContextReferenceRecord['kind']): AttachmentChipVisual {
  switch (kind) {
    case 'research-report':
      return { chipClassName: 'border-brand/25 bg-brand-subtle text-brand', icon: FileText }
    case 'chat-rule':
      return { chipClassName: 'border-success/25 bg-success/10 text-success', icon: BookOpen }
    case 'file-asset':
      return { chipClassName: 'border-file/25 bg-file-subtle text-file', icon: Paperclip }
    case 'file-group':
      return { chipClassName: 'border-file/25 bg-file-subtle text-file', icon: FolderOpen }
  }
}
