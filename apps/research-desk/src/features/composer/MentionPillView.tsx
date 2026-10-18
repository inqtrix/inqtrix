import { useEffect, useReducer } from 'react'
import { NodeViewWrapper, type NodeViewProps } from '@tiptap/react'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { cn } from '@/lib/utils'
import { MENTION_PILL_NAME } from './MentionPill'

const PILL_CLASS: Record<string, string> = {
  'file-asset': 'border-file/30 bg-file-subtle text-file',
  'file-group': 'border-file/30 bg-file-subtle text-file',
  'research-report': 'border-brand/30 bg-brand-subtle text-brand',
}

/**
 * Renders one `[N]` pill. The number is the 1-based reading-order index of this
 * pill among all mention pills in the document. Because the index depends on
 * *other* pills, this node's own attributes do not change when a sibling pill is
 * inserted, removed or moved, so ProseMirror would not re-render the NodeView on
 * its own (the editor runs with `shouldRerenderOnTransaction` disabled). We
 * therefore force a re-render on every document update so the numbering stays in
 * sync.
 */
export function MentionPillView({ editor, getPos, node }: NodeViewProps) {
  const [, renumber] = useReducer((value: number) => value + 1, 0)
  useEffect(() => {
    editor.on('update', renumber)
    return () => {
      editor.off('update', renumber)
    }
  }, [editor])

  const pos = typeof getPos === 'function' ? getPos() : null
  let index = 0
  if (pos != null) {
    editor.state.doc.descendants((descendant, descendantPos) => {
      if (descendant.type.name === MENTION_PILL_NAME && descendantPos < pos) index += 1
    })
  }
  const number = index + 1
  const refKind = String(node.attrs.refKind)
  const refLabel = String(node.attrs.refLabel || node.attrs.refId)

  return (
    <NodeViewWrapper as="span" className="inline-flex align-baseline">
      <Tooltip>
        <TooltipTrigger asChild>
          <span
            className={cn(
              'mx-0.5 inline-flex select-none items-center rounded-[0.3rem] border px-1 text-[0.78em] font-semibold leading-tight',
              PILL_CLASS[refKind] ?? 'border-border bg-muted text-muted-foreground',
            )}
            contentEditable={false}
            data-mention-pill-number={number}
          >
            [{number}]
          </span>
        </TooltipTrigger>
        <TooltipContent side="top">{refLabel}</TooltipContent>
      </Tooltip>
    </NodeViewWrapper>
  )
}
