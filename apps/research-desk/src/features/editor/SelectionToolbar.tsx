import type { Editor } from '@tiptap/react'
import {
  Bold,
  Check,
  ChevronDown,
  Code2,
  Heading1,
  Heading2,
  Heading3,
  Highlighter,
  Italic,
  List,
  ListOrdered,
  ListTodo,
  MessageSquarePlus,
  Quote,
  Sparkles,
  Strikethrough,
  Type,
  Underline,
  type LucideIcon,
} from '@/components/icons'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { Separator } from '@/components/ui/separator'
import { cn } from '@/lib/utils'
import { runBlockAction, type BlockActionId } from './blockActions'

/** Localized strings for the selection toolbar, assembled from `editorCopy` in
 * `editorCopy.ts` (DE + EN) so all copy stays in one place. Block-type labels
 * reuse the existing `slash*` keys; `turnInto` reuses `blockTurnInto`. */
export type SelectionToolbarLabels = {
  comment: string
  privateComment?: string
  turnInto: string
  bold: string
  italic: string
  underline: string
  strike: string
  code: string
  highlight: string
  text: string
  heading1: string
  heading2: string
  heading3: string
  bulletList: string
  orderedList: string
  taskList: string
  blockquote: string
  codeBlock: string
}

/** Block types offered by "Turn into" — the slash menu's "Stil" group only
 * (Tabelle/Trenner are inserts, not conversions). `id` drives `runBlockAction`
 * (shared with the slash menu); `labelKey` maps into `SelectionToolbarLabels`. */
const TURN_INTO: { id: BlockActionId; icon: LucideIcon; labelKey: keyof SelectionToolbarLabels; isActive: (editor: Editor) => boolean }[] = [
  { id: 'paragraph', icon: Type, labelKey: 'text', isActive: (editor) => editor.isActive('paragraph') },
  { id: 'heading1', icon: Heading1, labelKey: 'heading1', isActive: (editor) => editor.isActive('heading', { level: 1 }) },
  { id: 'heading2', icon: Heading2, labelKey: 'heading2', isActive: (editor) => editor.isActive('heading', { level: 2 }) },
  { id: 'heading3', icon: Heading3, labelKey: 'heading3', isActive: (editor) => editor.isActive('heading', { level: 3 }) },
  { id: 'bulletList', icon: List, labelKey: 'bulletList', isActive: (editor) => editor.isActive('bulletList') },
  { id: 'orderedList', icon: ListOrdered, labelKey: 'orderedList', isActive: (editor) => editor.isActive('orderedList') },
  { id: 'taskList', icon: ListTodo, labelKey: 'taskList', isActive: (editor) => editor.isActive('taskList') },
  { id: 'blockquote', icon: Quote, labelKey: 'blockquote', isActive: (editor) => editor.isActive('blockquote') },
  { id: 'codeBlock', icon: Code2, labelKey: 'codeBlock', isActive: (editor) => editor.isActive('codeBlock') },
]

/** First non-paragraph block that is active (a list item also matches
 * `paragraph`, so paragraph is the fallback, never the match). */
function activeTurnInto(editor: Editor) {
  return TURN_INTO.find((block) => block.id !== 'paragraph' && block.isActive(editor)) ?? TURN_INTO[0]
}

/**
 * Formatting cluster of the selection bubble menu (live mode). Markdown-safe
 * controls only — Bold/Italic/Underline/Strike/Code/Highlight (all
 * round-trip through `@tiptap/markdown`) plus a "Turn into" block-type switcher
 * (`runBlockAction`, shared with the slash menu) and a prominent Comment entry.
 * No text colour / alignment / super-subscript (those are lost in GFM). The
 * comment composer + submit live in the parent `EditorBubbleMenu`; this only
 * raises `onStartComment`. `onInteractingChange` keeps the bubble open while the
 * "Turn into" dropdown is open (Tiptap warns interactive children otherwise
 * close the menu).
 */
export function SelectionToolbar({
  commentOnly = false,
  editor,
  labels,
  onStartComment,
  onInteractingChange,
  privateCommentEnabled = true,
  teamCommentEnabled = false,
}: {
  commentOnly?: boolean
  editor: Editor
  labels: SelectionToolbarLabels
  onStartComment: (visibility: 'private' | 'team') => void
  onInteractingChange: (interacting: boolean) => void
  privateCommentEnabled?: boolean
  teamCommentEnabled?: boolean
}) {
  const active = activeTurnInto(editor)
  const ActiveIcon = active.icon

  return (
    <div className="flex min-w-0 items-center gap-0.5">
      <div className="flex items-center">
        <Button
          className={cn(
            'h-6 gap-1 px-1.5',
            teamCommentEnabled && privateCommentEnabled && 'rounded-r-none',
          )}
          onClick={() => onStartComment(teamCommentEnabled ? 'team' : 'private')}
          size="sm"
          type="button"
          variant="ghost"
        >
          <MessageSquarePlus className="size-3.5" />
          {labels.comment}
        </Button>
        {teamCommentEnabled && privateCommentEnabled ? (
          <DropdownMenu onOpenChange={onInteractingChange}>
            <DropdownMenuTrigger asChild>
              <Button
                aria-label={labels.privateComment}
                className="size-6 rounded-l-none border-l border-border"
                size="icon"
                type="button"
                variant="ghost"
              >
                <ChevronDown className="size-3" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="start" className="w-48">
              <DropdownMenuItem onSelect={() => onStartComment('private')}>
                <Sparkles className="size-4 text-muted-foreground" />
                {labels.privateComment}
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        ) : null}
      </div>
      {commentOnly ? null : (
        <>
          <Separator className="mx-0.5 h-4" orientation="vertical" />

          <DropdownMenu onOpenChange={onInteractingChange}>
            <DropdownMenuTrigger asChild>
              <Button aria-label={labels.turnInto} className="h-6 gap-1 px-1.5" size="sm" type="button" variant="ghost">
                <ActiveIcon className="size-3.5" />
                <span className="max-w-[7rem] truncate">{labels[active.labelKey]}</span>
                <ChevronDown className="size-3 opacity-60" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="start" className="w-52">
              {TURN_INTO.map((block) => {
                const Icon = block.icon
                const isActive = block.id === active.id
                return (
                  <DropdownMenuItem
                    className={cn(isActive && 'text-brand')}
                    key={block.id}
                    onSelect={() => runBlockAction(editor, block.id)}
                  >
                    <Icon className="size-4 text-muted-foreground" />
                    {labels[block.labelKey]}
                    {isActive ? <Check className="ml-auto size-4" /> : null}
                  </DropdownMenuItem>
                )
              })}
            </DropdownMenuContent>
          </DropdownMenu>
          <Separator className="mx-0.5 h-4" orientation="vertical" />

          <MiniToolbarButton active={editor.isActive('bold')} icon={Bold} label={labels.bold} onClick={() => editor.chain().focus().toggleBold().run()} />
          <MiniToolbarButton active={editor.isActive('italic')} icon={Italic} label={labels.italic} onClick={() => editor.chain().focus().toggleItalic().run()} />
          <MiniToolbarButton active={editor.isActive('underline')} icon={Underline} label={labels.underline} onClick={() => editor.chain().focus().toggleUnderline().run()} />
          <MiniToolbarButton active={editor.isActive('strike')} icon={Strikethrough} label={labels.strike} onClick={() => editor.chain().focus().toggleStrike().run()} />
          <MiniToolbarButton active={editor.isActive('code')} icon={Code2} label={labels.code} onClick={() => editor.chain().focus().toggleCode().run()} />
          <Separator className="mx-0.5 h-4" orientation="vertical" />

          <MiniToolbarButton active={editor.isActive('highlight')} icon={Highlighter} label={labels.highlight} onClick={() => editor.chain().focus().toggleHighlight().run()} />
        </>
      )}
    </div>
  )
}

/** Icon-only ghost toggle used across the selection toolbar. Brand-subtle fill
 * marks the active state (the brand accent is reserved for selection and
 * the primary action). No `.t-*` role — `Button` carries its own control
 * size, and a role class would lose to the button's text utility. */
function MiniToolbarButton({
  active,
  icon: Icon,
  label,
  onClick,
}: {
  active?: boolean
  icon: LucideIcon
  label: string
  onClick: () => void
}) {
  return (
    <Button
      aria-label={label}
      aria-pressed={active}
      className={cn('size-6 rounded-md', active && 'bg-brand-subtle text-brand')}
      onClick={onClick}
      size="icon"
      type="button"
      variant="ghost"
    >
      <Icon className="size-3.5" />
    </Button>
  )
}
