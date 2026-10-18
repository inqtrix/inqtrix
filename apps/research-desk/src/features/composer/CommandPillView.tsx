import { useContext } from 'react'
import { NodeViewWrapper, type NodeViewProps } from '@tiptap/react'

import { BookOpen, Database, FileText, MessagesSquare, PenLine, Sparkles } from '@/components/icons'
import { useLocale } from '@/i18n/LocaleProvider'
import { cn } from '@/lib/utils'

import type { CommandPillKind } from './CommandPill'
import { CommandPillStateContext, commandPillKey } from './commandPillState'

const PILL_CLASS: Record<CommandPillKind, string> = {
  knowledge: 'border-brand/35 bg-brand-subtle text-brand',
  responseForm: 'border-brand/35 bg-brand-subtle text-brand',
  skill: 'border-success/35 bg-success-subtle text-success',
  // The chat's rule tone and glyph, so a rule reads the same everywhere.
  rule: 'border-success/35 bg-success-subtle text-success',
  // The chat's report and file tones, so a document reads the same everywhere.
  report: 'border-brand/35 bg-brand-subtle text-brand',
  canvas: 'border-brand/35 bg-brand-subtle text-brand',
  editTarget: 'border-file/35 bg-file-subtle text-file',
}

/** A capsule the selected engine refuses, or whose document is gone. */
const WARNING_CLASS = 'border-warning/50 bg-warning-subtle text-warning'

function PillIcon({ kind, label }: { kind: CommandPillKind; label: string }) {
  if (kind === 'skill') return <Sparkles aria-hidden="true" className="size-3 shrink-0" />
  if (kind === 'rule') return <BookOpen aria-hidden="true" className="size-3 shrink-0" />
  if (kind === 'knowledge') return <Database aria-hidden="true" className="size-3 shrink-0" />
  if (kind === 'editTarget') return <PenLine aria-hidden="true" className="size-3 shrink-0" />
  if (kind === 'report' || kind === 'canvas') {
    return <FileText aria-hidden="true" className="size-3 shrink-0" />
  }
  // The response form names itself: the chat form gets the chat glyph.
  return label.toLowerCase() === 'chat'
    ? <MessagesSquare aria-hidden="true" className="size-3 shrink-0" />
    : <FileText aria-hidden="true" className="size-3 shrink-0" />
}

/** What a screen reader hears for a pill: its kind, its title and how it
 * goes — inside the textbox the bare title sounded like a typed word. */
function pillAriaLabel(
  kind: CommandPillKind,
  label: string,
  t: ReturnType<typeof useLocale>['t'],
): string {
  const composer = t.agent.composer
  const templates: Record<CommandPillKind, string> = {
    canvas: composer.pillAriaCanvas,
    editTarget: composer.pillAriaEditTarget,
    knowledge: composer.pillAriaKnowledge,
    report: composer.pillAriaReport,
    responseForm: composer.pillAriaResponseForm,
    rule: composer.pillAriaRule,
    skill: composer.pillAriaSkill,
  }
  // A function replacer: the label is user text, and a string replacement
  // would read `$&` in it as a pattern.
  return (templates[kind] ?? composer.pillAriaResponseForm).replace('{label}', () => label)
}

/**
 * One inline command pill. It sits in the text flow at the position the user
 * typed the command, is not text-editable, and is removed like a character —
 * the composer turns that removal into "switch off" or "detach".
 *
 * A long title (a report is titled by its whole research question) shows
 * shortened with an ellipsis; the tooltip and the screen reader carry all of
 * it, and the question spells all of it.
 */
export function CommandPillView({ node }: NodeViewProps) {
  const { t } = useLocale()
  const state = useContext(CommandPillStateContext)
  const kind = String(node.attrs.pillKind) as CommandPillKind
  const label = String(node.attrs.pillLabel)
  const unavailable = state.unavailable.includes(commandPillKey(kind, String(node.attrs.pillId ?? '')))
  const refused = state.refusedKinds.includes(kind)
  const named = unavailable ? `${label} (${t.agent.composer.pillUnavailable})` : label
  return (
    <NodeViewWrapper as="span" className="inline-flex max-w-full align-baseline">
      {/* role img: a generic span may not carry a name, an atom with this
          role may — the glyph and the visible title are then read once,
          as that name. */}
      <span
        aria-describedby={refused ? state.refusalNoticeId : undefined}
        aria-label={pillAriaLabel(kind, named, t)}
        className={cn(
          'mx-0.5 inline-flex max-w-full select-none items-center gap-1 rounded-[0.35rem] border px-1.5 py-px text-[0.85em] font-medium leading-tight',
          unavailable || refused
            ? WARNING_CLASS
            : PILL_CLASS[kind] ?? 'border-border bg-muted text-muted-foreground',
        )}
        contentEditable={false}
        data-command-pill-kind={kind}
        data-command-pill-label={label}
        data-command-pill-unavailable={unavailable ? 'true' : undefined}
        data-engine-refused={refused ? 'true' : undefined}
        role="img"
        title={label}
      >
        <PillIcon kind={kind} label={label} />
        <span className="min-w-0 max-w-[16rem] truncate">{label}</span>
        {unavailable && (
          <span className="shrink-0 font-normal">· {t.agent.composer.pillUnavailable}</span>
        )}
      </span>
    </NodeViewWrapper>
  )
}
