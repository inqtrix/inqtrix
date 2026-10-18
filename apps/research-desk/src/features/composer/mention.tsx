import { BookOpen, FileText, FolderOpen, Paperclip, type LucideIcon } from '@/components/icons'
import {
  MentionMenu,
  type MentionMenuLabels,
  type MentionMenuOption,
  type MentionMenuScope,
  type MentionTone,
} from '@/components/ui/mention-menu'
import { useLocale } from '@/i18n/LocaleProvider'
import type {
  ChatRuleOption,
  CompletedReportOption,
  FileGroupMentionOption,
  FileMentionOption,
} from '@/features/project/selectors'
import type { ChatContextReferenceRecord, ChatRuleCategory } from '@/features/project/types'

export type MentionKind = 'research' | 'rules' | 'files' | 'filegroups'

export type MentionMatch = {
  end: number
  kind: MentionKind | 'root'
  query: string
  start: number
}

export type MentionCategoryLabels = {
  files: string
  filegroups: string
  research: string
  rules: string
}

export type MentionSources = {
  fileGroupOptions: FileGroupMentionOption[]
  fileOptions: FileMentionOption[]
  reportOptions: CompletedReportOption[]
  ruleOptions: ChatRuleOption[]
}

export type MentionOption = {
  category?: ChatRuleCategory
  label: string
  prefix?: '@research:' | '@rules:' | '@files:' | '@filegroups:'
  ref?: ChatContextReferenceRecord
  title: string
  type: MentionKind
}

const ALL_KINDS: MentionKind[] = ['research', 'rules', 'files', 'filegroups']

const CATEGORY_PREFIX: Record<MentionKind, MentionOption['prefix']> = {
  research: '@research:',
  rules: '@rules:',
  files: '@files:',
  filegroups: '@filegroups:',
}

/**
 * Display order for grouped rule items. Sorting rule options into this order at
 * build time keeps the flat option list aligned with the grouped rendering, so
 * the menu's active index and arrow navigation stay in visual order without a
 * separate index-reconciliation step at render time.
 */
const RULE_CATEGORY_ORDER: Record<ChatRuleCategory, number> = {
  instruction: 0,
  function: 1,
  context: 2,
}

/**
 * Build mention options for the current trigger. `enabledKinds` restricts which
 * categories are offered (the chat and the editor composer both enable all
 * four today). With a single enabled kind the root "@" lists that kind's items
 * directly instead of an intermediate category step.
 */
export function buildMentionOptions(
  match: MentionMatch,
  sources: MentionSources,
  categoryLabels: MentionCategoryLabels,
  enabledKinds: MentionKind[] = ALL_KINDS,
): MentionOption[] {
  const query = match.query.toLowerCase()
  if (match.kind === 'root') {
    if (enabledKinds.length === 1) {
      return mentionItemsForKind(enabledKinds[0], '', sources)
    }
    return enabledKinds.map((kind) => ({
      label: CATEGORY_PREFIX[kind] ?? `@${kind}:`,
      prefix: CATEGORY_PREFIX[kind],
      title: categoryLabels[kind],
      type: kind,
    }))
  }
  return enabledKinds.includes(match.kind)
    ? mentionItemsForKind(match.kind, query, sources)
    : []
}

function mentionItemsForKind(
  kind: MentionKind,
  query: string,
  sources: MentionSources,
): MentionOption[] {
  if (kind === 'research') {
    return sources.reportOptions
      .filter((report) => matchesMentionQuery(query, report.label, report.title))
      .slice(0, 8)
      .map((report) => ({
        label: report.label,
        ref: { kind: 'research-report', runId: report.runId },
        title: report.title,
        type: 'research' as const,
      }))
  }
  if (kind === 'files') {
    return sources.fileOptions
      .filter((file) => matchesMentionQuery(query, file.label, file.title))
      .slice(0, 8)
      .map((file) => ({
        label: file.label,
        ref: { fileId: file.fileId, kind: 'file-asset' },
        title: file.title,
        type: 'files' as const,
      }))
  }
  if (kind === 'filegroups') {
    return sources.fileGroupOptions
      .filter((group) => matchesMentionQuery(query, group.label, group.title))
      .slice(0, 8)
      .map((group) => ({
        label: group.label,
        ref: { groupId: group.groupId, kind: 'file-group' },
        title: group.title,
        type: 'filegroups' as const,
      }))
  }
  return sources.ruleOptions
    .filter((rule) => matchesMentionQuery(query, rule.label, rule.title))
    .sort((a, b) => RULE_CATEGORY_ORDER[a.category] - RULE_CATEGORY_ORDER[b.category])
    .slice(0, 8)
    .map((rule) => ({
      category: rule.category,
      label: rule.label,
      ref: { kind: 'chat-rule', ruleId: rule.ruleId },
      title: rule.title,
      type: 'rules' as const,
    }))
}

const MENTION_KIND_BY_TOKEN: Record<string, MentionKind> = {
  research: 'research',
  rules: 'rules',
  files: 'files',
  filegroups: 'filegroups',
}

export function detectMentionTrigger(value: string, cursor: number): MentionMatch | null {
  const prefix = value.slice(0, cursor)
  const rootMatch = /(?:^|\s)(@)$/i.exec(prefix)
  if (rootMatch) {
    return { end: cursor, kind: 'root', query: '', start: cursor - rootMatch[1].length }
  }
  const match = /(?:^|\s)(@(rules|research|files|filegroups):([a-z0-9-]*))$/i.exec(prefix)
  if (!match) return null
  return {
    end: cursor,
    kind: MENTION_KIND_BY_TOKEN[match[2].toLowerCase()] ?? 'rules',
    query: match[3].toLowerCase(),
    start: cursor - match[1].length,
  }
}

function matchesMentionQuery(query: string, label: string, title: string) {
  if (!query) return true
  return label.includes(query) || title.toLowerCase().includes(query)
}

const MENTION_ICON: Record<MentionKind, LucideIcon> = {
  research: FileText,
  rules: BookOpen,
  files: Paperclip,
  filegroups: FolderOpen,
}

const MENTION_TONE: Record<MentionKind, MentionTone> = {
  research: 'brand',
  rules: 'success',
  files: 'file',
  filegroups: 'warning',
}

/**
 * Adapter between the mention domain model and the presentational `MentionMenu`.
 * It maps `MentionOption`s to the menu's tone/icon/group vocabulary, derives the
 * breadcrumb scope from the active trigger, and resolves the i18n labels. All
 * trigger, keyboard, and selection logic stays in `MentionComposer`; this layer
 * only translates data and forwards intents (select/hover/back).
 */
export function MentionAutocomplete({
  activeIndex,
  match,
  onBack,
  onHover,
  onSelect,
  options,
}: {
  activeIndex: number
  match: MentionMatch
  onBack: () => void
  onHover: (index: number) => void
  onSelect: (option: MentionOption) => void
  options: MentionOption[]
}) {
  const { t } = useLocale()
  const ruleGroupLabels: Record<ChatRuleCategory, string> = {
    context: t.promptLibrary.categoryContext,
    function: t.promptLibrary.categoryFunction,
    instruction: t.promptLibrary.categoryInstruction,
  }
  const labels: MentionMenuLabels = {
    backHint: t.chat.mentionBackHint,
    closeHint: t.chat.mentionCloseHint,
    filterPlaceholder: t.chat.mentionFilterPlaceholder,
    navHint: t.chat.mentionNavHint,
    rootTitle: t.chat.mentionResults,
    selectHint: t.chat.mentionSelectHint,
  }
  const menuOptions: MentionMenuOption[] = options.map((option) => {
    const isCategory = Boolean(option.prefix)
    return {
      group: !isCategory && option.type === 'rules' && option.category ? ruleGroupLabels[option.category] : undefined,
      icon: MENTION_ICON[option.type],
      isCategory,
      // Category rows lead with the `@kind:` token (the key info when picking a
      // category); item rows lead with the human title and keep the token as a
      // secondary hint. The token line is rendered in mono by MentionMenu.
      primary: isCategory ? option.label : option.title,
      secondary: isCategory ? option.title : `@${option.type}:${option.label}`,
      tone: MENTION_TONE[option.type],
    }
  })
  const scope: MentionMenuScope =
    match.kind === 'root'
      ? { kind: null, query: '' }
      : {
          icon: MENTION_ICON[match.kind],
          kind: `@${match.kind}:`,
          query: match.query,
          tone: MENTION_TONE[match.kind],
        }
  return (
    <MentionMenu
      activeIndex={activeIndex}
      labels={labels}
      onBack={onBack}
      onHover={onHover}
      onSelect={(index) => onSelect(options[index])}
      options={menuOptions}
      scope={scope}
    />
  )
}
