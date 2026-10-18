/**
 * Read-only document diff view: renders the block-level diff between a pinned
 * comparison anchor and the current markdown (`documentDiffPlan`), with
 * word-level inline segments for lightly edited paragraphs. Rendered by
 * `core/MarkdownEditorSurface` while the diff toggle is active.
 */
import { Fragment, type ReactNode } from 'react'
import { MarkdownRenderer } from '@/components/markdown/MarkdownRenderer'
import { Scale } from '@/components/icons'
import { ScrollArea } from '@/components/ui/scroll-area'
import { cn } from '@/lib/utils'
import { documentDiffPlan, type DocumentDiffBlock, type SuggestionDiffSegment } from './suggestionDiff'
import type { EditorCopy } from './editorCopy'

export type DocumentDiffViewProps = {
  anchorMarkdown: string | null
  copy: Pick<EditorCopy, 'noDiffAnchor' | 'diffView'>
  currentMarkdown: string
}

export function DocumentDiffView({
  anchorMarkdown,
  copy,
  currentMarkdown,
}: DocumentDiffViewProps) {
  if (!anchorMarkdown) {
    return (
      <div className="flex min-h-0 flex-1 items-center justify-center bg-background p-8">
        <div className="rounded-md border border-dashed border-border px-4 py-3 text-sm text-muted-foreground">
          {copy.noDiffAnchor}
        </div>
      </div>
    )
  }
  const blocks = documentDiffPlan(anchorMarkdown, currentMarkdown)
  return (
    <ScrollArea className="min-h-0 flex-1 bg-background">
      <div className="editor-document-diff mx-auto min-h-full max-w-[72rem] px-4 py-6 sm:px-10 sm:py-8">
        <div className="t-caption mb-3 flex items-center gap-2 text-brand">
          <Scale className="size-3.5" />
          {copy.diffView}
        </div>
        <div className="editor-document-diff-body editor-prose">
          {blocks.map((block, index) => (
            <EditorDocumentDiffBlock block={block} index={index} key={documentDiffBlockKey(block, index)} />
          ))}
        </div>
      </div>
    </ScrollArea>
  )
}

function EditorDocumentDiffBlock({ block, index }: { block: DocumentDiffBlock; index: number }) {
  if (block.kind === 'replace') {
    if (block.inlineSegments) {
      return (
        <div className="editor-document-diff-replace editor-document-diff-replace-inline">
          <p className="editor-document-diff-inline-row">
            {block.inlineSegments.map((segment, segmentIndex) => (
              <EditorDocumentDiffInlineSegment
                key={`${index}-${segmentIndex}-${segment.type}-${segment.text.length}`}
                segment={segment}
              />
            ))}
          </p>
        </div>
      )
    }

    return (
      <div className="editor-document-diff-replace editor-document-diff-replace-structured">
        <div className="editor-document-diff-layer editor-document-diff-delete">
          <MarkdownRenderer markdown={block.beforeMarkdown} variant="report" />
        </div>
        <div className="editor-document-diff-layer editor-document-diff-insert">
          <MarkdownRenderer markdown={block.afterMarkdown} variant="report" />
        </div>
      </div>
    )
  }

  return (
    <div
      className={cn(
        'editor-document-diff-chunk',
        block.kind === 'equal' && 'editor-document-diff-equal',
        block.kind === 'insert' && 'editor-document-diff-layer editor-document-diff-insert',
        block.kind === 'delete' && 'editor-document-diff-layer editor-document-diff-delete',
      )}
    >
      <MarkdownRenderer markdown={block.markdown} variant="report" />
    </div>
  )
}

function EditorDocumentDiffInlineSegment({ segment }: { segment: SuggestionDiffSegment }) {
  if (segment.type === 'insert') {
    return (
      <ins className="editor-document-diff-token editor-document-diff-token-insert">
        {renderInlineMarkdownText(segment.text)}
      </ins>
    )
  }
  if (segment.type === 'delete') {
    return (
      <del className="editor-document-diff-token editor-document-diff-token-delete">
        {renderInlineMarkdownText(segment.text)}
      </del>
    )
  }
  return (
    <span className="editor-document-diff-token">
      {renderInlineMarkdownText(segment.text)}
    </span>
  )
}

function documentDiffBlockKey(block: DocumentDiffBlock, index: number): string {
  if (block.kind === 'replace') {
    return `${block.kind}-${index}-${block.beforeMarkdown.length}-${block.afterMarkdown.length}`
  }
  return `${block.kind}-${index}-${block.markdown.length}`
}

function renderInlineMarkdownText(text: string): ReactNode[] {
  const nodes: ReactNode[] = []
  const tokenPattern = /(\[[^\]\n]+\]\([^) \n]+(?:\s+"[^"\n]*")?\)|`[^`\n]+`|\*\*[^*\n][^*\n]*\*\*|\*[^*\n][^*\n]*\*)/g
  let cursor = 0
  let match: RegExpExecArray | null
  while ((match = tokenPattern.exec(text))) {
    if (match.index > cursor) {
      nodes.push(<Fragment key={`text-${cursor}`}>{text.slice(cursor, match.index)}</Fragment>)
    }
    nodes.push(renderInlineMarkdownToken(match[0], match.index))
    cursor = match.index + match[0].length
  }
  if (cursor < text.length) {
    nodes.push(<Fragment key={`text-${cursor}`}>{text.slice(cursor)}</Fragment>)
  }
  return nodes
}

function renderInlineMarkdownToken(token: string, index: number): ReactNode {
  const link = token.match(/^\[([^\]\n]+)\]\(([^) \n]+)(?:\s+"[^"\n]*")?\)$/)
  if (link) {
    return (
      <a
        className="editor-document-diff-inline-link"
        href={link[2]}
        key={`link-${index}`}
        rel="noreferrer"
        target="_blank"
      >
        {link[1]}
      </a>
    )
  }
  if (token.startsWith('`') && token.endsWith('`')) {
    return <code className="editor-document-diff-inline-code" key={`code-${index}`}>{token.slice(1, -1)}</code>
  }
  if (token.startsWith('**') && token.endsWith('**')) {
    return <strong key={`strong-${index}`}>{token.slice(2, -2)}</strong>
  }
  if (token.startsWith('*') && token.endsWith('*')) {
    return <em key={`em-${index}`}>{token.slice(1, -1)}</em>
  }
  return <Fragment key={`token-${index}`}>{token}</Fragment>
}
