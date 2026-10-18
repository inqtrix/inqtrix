import { useEffect, useMemo, useState, type Dispatch, type ReactNode } from 'react'
import { useReducedMotion } from 'motion/react'
import {
  AlertTriangle,
  BookOpen,
  Bot,
  Check,
  ChevronLeft,
  EyeOff,
  FileText,
  FolderOpen,
  Library,
  ListOrdered,
  MessagesSquare,
  Sparkles,
  Paperclip,
  Plus,
  Save,
  Search,
  Trash2,
  X,
  type LucideIcon,
  Users,
} from '@/components/icons'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { ScrollArea } from '@/components/ui/scroll-area'
import { Textarea } from '@/components/ui/textarea'
import {
  TextImproveButton,
  TextImproveFieldLayer,
  useTextImprovement,
  type TextImprovementApiOptions,
} from '@/features/textImprove'
import {
  chatAttachmentChipsFromRefs,
  chatContextRefKey,
  dedupeChatContextRefs,
  fileGroupMentionOptions,
  fileMentionOptions,
  projectChatRules,
} from '@/features/project/selectors'
import {
  chatRuleCategories,
  compareChatRulesByCategory,
  normalizeChatRule,
  normalizeLinkedContextRefs,
} from '@/features/project/chatRules'
import {
  contextPackPlaceholder,
  renderChatRuleAttachmentContent,
} from '@/features/project/chatRuleRendering'
import type {
  ChatContextReferenceRecord,
  ChatRuleCategory,
  ChatRuleRecord,
  ChatRuleVisibility,
  ProjectState,
} from '@/features/project/types'
import type { ResearchDeskAction } from '@/features/researchDesk/state'
import {
  TemplateConflictError,
  canDeleteRule,
  canEditRule,
  canSavePromptDraft,
  hasPromptDraftConflict,
} from './templateSync'
import type { TemplateSyncHandle } from './useTemplateSync'
import { SkillLibraryPanel } from '@/features/skills/SkillLibraryPanel'
import type { SkillInfo } from '@/features/skills/skillLibrary'
import type { SkillsApiHandle } from '@/features/skills/useSkillsApi'
import { useLocale } from '@/i18n/LocaleProvider'
import { countLabel } from '@/i18n/plural'
import { cn } from '@/lib/utils'
import {
  toneAccentBorderLeft,
  toneActiveCard,
  toneBar,
  toneText,
  type MentionTone,
} from '@/lib/tone'
import { createRuleId, normalizeRuleLabel } from '@/features/chat/rules/ruleLabels'

type CategoryFilter = ChatRuleCategory | 'all'
type VisibilityFilter = 'all' | 'chat' | 'editor' | 'hidden'

type PromptDraft = {
  baseRevision: number | null
  category: ChatRuleCategory
  contentMarkdown: string
  error: string | null
  includeInAutocomplete: boolean
  isDirty: boolean
  label: string
  linkedContextRefs: ChatContextReferenceRecord[]
  selectedRuleId: string | null
  /** Immutable server destination of the draft loaded from a synced rule. */
  sourceTemplateId: string | null
  title: string
  visibility: ChatRuleVisibility
}

type ContextPickerOption = {
  icon: LucideIcon
  key: string
  label: string
  ref: ChatContextReferenceRecord
  title: string
  type: 'file' | 'group'
}

const contextPickerLimit = 8

const emptyDraft: PromptDraft = {
  baseRevision: null,
  category: 'instruction',
  contentMarkdown: '',
  error: null,
  includeInAutocomplete: true,
  isDirty: false,
  label: '',
  linkedContextRefs: [],
  selectedRuleId: null,
  sourceTemplateId: null,
  title: '',
  visibility: { agent: false, chat: true, editor: true },
}

export function PromptLibraryWorkspace({
  dispatch,
  onRequestedResourceHandled,
  requestedResource = null,
  sharing = null,
  skillsApi = null,
  state,
  templateSync = null,
  textImprovement,
}: {
  dispatch: Dispatch<ResearchDeskAction>
  onRequestedResourceHandled?: () => void
  requestedResource?: {
    resourceId: string
    resourceType: 'prompt_template' | 'skill_template'
  } | null
  sharing?: {
    onShareRule: (rule: ChatRuleRecord) => void
    onShareSkill: (skill: SkillInfo) => void
  } | null
  /** Skill library handle; null hides the Skills tab
   * (feature off or server absent). */
  skillsApi?: SkillsApiHandle | null
  state: ProjectState
  templateSync?: TemplateSyncHandle | null
  textImprovement: Omit<TextImprovementApiOptions, 'locale'>
}) {
  const { locale, t } = useLocale()
  const reduceMotion = useReducedMotion()
  const rules = useMemo(
    () => projectChatRules(state).toSorted(compareChatRulesByCategory),
    [state.chatRuleOrder, state.chatRules],
  )
  const [categoryFilter, setCategoryFilter] = useState<CategoryFilter>('all')
  const [visibilityFilter, setVisibilityFilter] = useState<VisibilityFilter>('all')
  const [query, setQuery] = useState('')
  const [contextQuery, setContextQuery] = useState('')
  const [isPreviewOpen, setPreviewOpen] = useState(false)
  const [promptImproveError, setPromptImproveError] = useState<string | null>(null)
  const [draft, setDraft] = useState<PromptDraft>(() => draftFromRule(rules[0] ?? null))
  const [pendingNav, setPendingNav] = useState<(() => void) | null>(null)
  const [isSaving, setIsSaving] = useState(false)
  const [isMobileDetailOpen, setIsMobileDetailOpen] = useState(false)
  const [libraryTab, setLibraryTab] = useState<'rules' | 'skills'>('rules')
  const promptTextImprove = useTextImprovement({
    ...textImprovement,
    locale,
    messages: {
      requestFailed: (message) => `${t.textImprove.requestFailed}: ${message}`,
      sensitiveText: t.textImprove.sensitiveText,
      unavailable: t.textImprove.unavailable,
    },
  })
  const selectedRule = draft.selectedRuleId
    ? rules.find((rule) => rule.id === draft.selectedRuleId)
      ?? (draft.sourceTemplateId
        ? rules.find((rule) => rule.serverTemplateId === draft.sourceTemplateId)
        : null)
      ?? null
    : null
  const sourceUnavailable = draft.sourceTemplateId !== null && selectedRule === null
  const remoteConflict = draft.isDirty && hasPromptDraftConflict(
    draft.sourceTemplateId,
    selectedRule,
    draft.baseRevision,
  )
  const permissionDowngraded = Boolean(
    draft.isDirty && selectedRule && !canEditRule(selectedRule),
  )
  const canSaveDraft = canSavePromptDraft(
    draft.sourceTemplateId,
    selectedRule,
    draft.baseRevision,
  )
  const fileOptions = fileMentionOptions(state)
  const fileGroupOptions = fileGroupMentionOptions(state)
  const contextOptions = useMemo(
    () => [
      ...fileGroupOptions.map<ContextPickerOption>((group) => ({
        icon: FolderOpen,
        key: `file-group:${group.groupId}`,
        label: `@filegroups:${group.label}`,
        ref: { groupId: group.groupId, kind: 'file-group' as const },
        title: group.title,
        type: 'group' as const,
      })),
      ...fileOptions.map<ContextPickerOption>((file) => ({
        icon: Paperclip,
        key: `file-asset:${file.fileId}`,
        label: `@files:${file.label}`,
        ref: { fileId: file.fileId, kind: 'file-asset' as const },
        title: file.title,
        type: 'file' as const,
      })),
    ],
    [fileGroupOptions, fileOptions],
  )
  const contextOptionByKey = useMemo(
    () => new Map(contextOptions.map((option) => [option.key, option])),
    [contextOptions],
  )
  const contextChips = chatAttachmentChipsFromRefs(state, draft.linkedContextRefs)
  const filteredRules = rules.filter((rule) => {
    const normalized = normalizeChatRule(rule)
    const visibility = normalized.visibility ?? { agent: false, chat: true, editor: true }
    const isAutocompleteVisible = normalized.includeInAutocomplete !== false
    const haystack = [
      normalized.label,
      normalized.title,
      normalized.contentMarkdown,
      categoryLabel(normalized.category ?? 'instruction', t),
    ].join(' ').toLowerCase()
    const trimmedQuery = query.trim().toLowerCase()
    const matchesQuery = !trimmedQuery || haystack.includes(trimmedQuery)
    const matchesCategory = categoryFilter === 'all' || normalized.category === categoryFilter
    const matchesVisibility = visibilityFilter === 'all'
      || (visibilityFilter === 'chat' && isAutocompleteVisible && visibility.chat)
      || (visibilityFilter === 'editor' && isAutocompleteVisible && visibility.editor)
      || (visibilityFilter === 'hidden' && (!isAutocompleteVisible || (!visibility.chat && !visibility.editor && !visibility.agent)))
    return matchesQuery && matchesCategory && matchesVisibility
  })
  const groupedRules = chatRuleCategories
    .map((category) => ({
      category,
      rules: filteredRules.filter((rule) => normalizeChatRule(rule).category === category),
    }))
    .filter((group) => group.rules.length > 0)
  const previewRule = draftRuleRecord(draft, selectedRule)
  const preview = renderChatRuleAttachmentContent(state, previewRule, new Date().toISOString())

  function loadRule(rule: ChatRuleRecord | null) {
    setDraft(draftFromRule(rule))
    setContextQuery('')
    setPromptImproveError(null)
    promptTextImprove.clearProposal()
    setPreviewOpen(false)
    setIsMobileDetailOpen(true)
  }

  function guardedLoad(rule: ChatRuleRecord | null) {
    if (draft.isDirty) {
      setPendingNav(() => () => loadRule(rule))
      return
    }
    loadRule(rule)
  }

  // Keep an untouched open draft synchronized with an authoritative refresh.
  // A dirty draft stays in place so optimistic concurrency can surface the
  // conflict instead of silently discarding the user's text.
  useEffect(() => {
    if (!draft.selectedRuleId || draft.isDirty) return
    if (!selectedRule) {
      setDraft(draftFromRule(rules[0] ?? null))
      return
    }
    setDraft(draftFromRule(selectedRule))
  }, [
    draft.isDirty,
    draft.selectedRuleId,
    rules,
    selectedRule?.access?.permission,
    selectedRule?.serverRevision,
    selectedRule?.updatedAt,
  ])

  useEffect(() => {
    if (!requestedResource || requestedResource.resourceType !== 'prompt_template') return
    const rule = rules.find(
      (candidate) => candidate.serverTemplateId === requestedResource.resourceId
        || candidate.id === requestedResource.resourceId,
    )
    if (!rule) return
    setLibraryTab('rules')
    guardedLoad(rule)
    onRequestedResourceHandled?.()
  }, [requestedResource, rules])

  useEffect(() => {
    if (requestedResource?.resourceType === 'skill_template' && skillsApi) {
      setLibraryTab('skills')
    }
  }, [requestedResource, skillsApi])

  function updateDraft(patch: Partial<PromptDraft>) {
    setDraft((current) => ({
      ...current,
      ...patch,
      error: patch.error === undefined ? null : patch.error,
      isDirty: patch.isDirty ?? true,
    }))
  }

  function setVisibility(key: keyof ChatRuleVisibility, value: boolean) {
    const visibility = { ...draft.visibility, [key]: value }
    updateDraft({
      includeInAutocomplete:
        visibility.chat || visibility.editor || visibility.agent,
      visibility,
    })
  }

  function toggleContextRef(ref: ChatContextReferenceRecord) {
    const key = chatContextRefKey(ref)
    const exists = draft.linkedContextRefs.some((item) => chatContextRefKey(item) === key)
    updateDraft({
      linkedContextRefs: exists
        ? draft.linkedContextRefs.filter((item) => chatContextRefKey(item) !== key)
        : dedupeChatContextRefs([...draft.linkedContextRefs, ref]),
    })
  }

  function removeContextRef(ref: ChatContextReferenceRecord) {
    const key = chatContextRefKey(ref)
    updateDraft({
      linkedContextRefs: draft.linkedContextRefs.filter((item) => chatContextRefKey(item) !== key),
    })
  }

  function keepPromptAsCopy() {
    setDraft((current) => ({
      ...current,
      baseRevision: null,
      error: null,
      isDirty: true,
      label: normalizeRuleLabel(`${current.label.replace(/-copy$/, '')}-copy`),
      selectedRuleId: createRuleId(),
      sourceTemplateId: null,
    }))
  }

  function discardUnavailableDraft() {
    loadRule(selectedRule)
  }

  async function savePrompt(): Promise<boolean> {
    if (sourceUnavailable) {
      updateDraft({
        error: t.promptLibrary.sourceUnavailable,
        isDirty: draft.isDirty,
      })
      return false
    }
    if (!canEditRule(selectedRule)) return false
    const label = normalizeRuleLabel(draft.label)
    const title = draft.title.trim() || label
    const contentMarkdown = draft.contentMarkdown.trim()
    if (!label) {
      updateDraft({ error: t.promptLibrary.labelRequired, isDirty: draft.isDirty })
      return false
    }
    if (rules.some((rule) => rule.label === label && rule.id !== draft.selectedRuleId)) {
      updateDraft({ error: t.promptLibrary.labelDuplicate, isDirty: draft.isDirty })
      return false
    }
    if (!contentMarkdown) {
      updateDraft({ error: t.promptLibrary.promptRequired, isDirty: draft.isDirty })
      return false
    }
    const now = new Date().toISOString()
    let rule = normalizeChatRule({
      access: selectedRule?.access,
      category: draft.category,
      contentMarkdown,
      createdAt: selectedRule?.createdAt ?? now,
      id: draft.selectedRuleId ?? createRuleId(),
      includeInAutocomplete: draft.includeInAutocomplete,
      label,
      linkedContextRefs: draft.category === 'context'
        ? normalizeLinkedContextRefs(draft.linkedContextRefs)
        : [],
      // Retain the original server destination independently of the live
      // list record. If that destination vanished, the guard above blocks
      // the save instead of silently creating an owned template.
      serverTemplateId: draft.sourceTemplateId ?? selectedRule?.serverTemplateId,
      // The draft keeps the revision it was actually based on. A background
      // resource refresh may update selectedRule while the user is editing;
      // borrowing that newer revision here would silently bypass OCC.
      serverRevision: draft.baseRevision ?? undefined,
      title,
      updatedAt: now,
      visibility: draft.visibility,
    })
    if (templateSync) {
      // Server-first write-through: the local dispatch happens only
      // after the server accepted the write (no silent divergence).
      // The isSaving guard blocks double-submits — two concurrent
      // POSTs would mint two server templates.
      if (isSaving) return false
      setIsSaving(true)
      try {
        rule = await templateSync.saveRule(rule)
      } catch (error) {
        // A refreshed conflict updates the authoritative list but never
        // lends its revision to this dirty draft. The conflict panel offers
        // only the two safe outcomes: create a copy or discard the draft.
        updateDraft({
          error: error instanceof TemplateConflictError
            ? error.refreshed
              ? null
              : t.promptLibrary.syncConflictStale
            : `${t.promptLibrary.syncFailed}: ${messageFromUnknown(error)}`,
          isDirty: draft.isDirty,
        })
        return false
      } finally {
        setIsSaving(false)
      }
      // Adoption re-keying: a first save keeps the browser-local id
      // while the server minted `pt_...`. Re-key the record to the
      // server id so the next hydrate upserts ONTO it instead of
      // inserting a duplicate twin.
      if (rule.serverTemplateId && rule.id !== rule.serverTemplateId) {
        const previousId = rule.id
        rule = normalizeChatRule({ ...rule, id: rule.serverTemplateId })
        if (draft.selectedRuleId === previousId) {
          dispatch({ ruleId: previousId, type: 'deleteChatRule' })
        }
      }
    }
    dispatch({ rule, type: 'upsertChatRule' })
    setDraft(draftFromRule(rule))
    return true
  }

  async function deletePrompt() {
    if (!selectedRule) return
    if (templateSync) {
      try {
        await templateSync.deleteRule(selectedRule)
      } catch (error) {
        updateDraft({
          error: `${t.promptLibrary.syncFailed}: ${messageFromUnknown(error)}`,
          isDirty: draft.isDirty,
        })
        return
      }
    }
    dispatch({ ruleId: selectedRule.id, type: 'deleteChatRule' })
    const next = rules.find((rule) => rule.id !== selectedRule.id) ?? null
    loadRule(next)
  }

  async function improvePrompt() {
    setPromptImproveError(null)
    try {
      await promptTextImprove.improve(
        'prompt_template',
        draft.contentMarkdown,
        promptImprovementGuidance(draft, t),
      )
    } catch (error) {
      setPromptImproveError(messageFromUnknown(error))
    }
  }

  function acceptPromptImprovement(contentMarkdown: string) {
    updateDraft({ contentMarkdown })
    promptTextImprove.clearProposal()
    setPromptImproveError(null)
  }

  const tabBar = skillsApi ? (
    <div className="flex items-center gap-1 border-b border-border bg-surface/50 px-4 py-2">
      {(['rules', 'skills'] as const).map((tab) => (
        <button
          className={cn(
            'rounded-md px-2.5 py-1 t-meta font-medium transition-colors',
            libraryTab === tab
              ? 'bg-background text-foreground shadow-[0_1px_2px_var(--shadow-hairline)]'
              : 'text-muted-foreground hover:text-foreground',
          )}
          key={tab}
          onClick={() => setLibraryTab(tab)}
          type="button"
        >
          {tab === 'rules' ? t.promptLibrary.rulesTab : t.skills.title}
          {tab === 'skills' && (
            <Badge className="ml-1.5" variant="outline">Beta</Badge>
          )}
        </button>
      ))}
    </div>
  ) : null

  if (skillsApi && libraryTab === 'skills') {
    return (
      <div className="flex h-[calc(100svh-var(--header-h))] min-h-0 flex-col bg-background">
        {tabBar}
        <SkillLibraryPanel
          api={skillsApi}
          onShare={sharing?.onShareSkill}
          onRequestedSkillHandled={onRequestedResourceHandled}
          reduceMotion={Boolean(reduceMotion)}
          requestedSkillId={requestedResource?.resourceType === 'skill_template'
            ? requestedResource.resourceId
            : null}
          textImprovement={textImprovement}
        />
      </div>
    )
  }

  return (
    <div className="flex h-[calc(100svh-var(--header-h))] min-h-0 flex-col bg-background">
      {tabBar}
      <div className="grid min-h-0 flex-1 lg:grid-cols-[320px_minmax(0,1fr)]">
      <aside
        className={cn(
          'min-h-0 min-w-0 flex-col border-b border-border bg-surface/50 lg:flex lg:border-b-0 lg:border-r',
          isMobileDetailOpen ? 'hidden' : 'flex',
        )}
      >
        <div className="border-b border-border p-4">
          <div className="flex items-center justify-between gap-3">
            <div className="flex min-w-0 items-center gap-2.5">
              <span className="grid size-9 place-items-center rounded-lg border border-brand/20 bg-brand-subtle text-brand">
                <Library className="size-4" />
              </span>
              <div className="min-w-0">
                <h1 className="t-title truncate text-foreground">{t.promptLibrary.title}</h1>
                <p className="t-meta truncate text-muted-foreground">{t.promptLibrary.subtitle}</p>
              </div>
            </div>
            <Button aria-label={t.promptLibrary.newPrompt} className="size-8" onClick={() => guardedLoad(null)} size="icon" type="button" variant="outline">
              <Plus className="size-4" />
            </Button>
          </div>
          <label className="mt-3 flex items-center gap-2 rounded-md border border-border bg-background px-2 focus-within:ring-2 focus-within:ring-ring">
            <Search className="size-4 shrink-0 text-muted-foreground" />
            <input
              className="min-w-0 flex-1 border-0 bg-transparent py-1.5 text-sm text-foreground outline-none"
              onChange={(event) => setQuery(event.target.value)}
              placeholder={t.promptLibrary.searchPlaceholder}
              value={query}
            />
          </label>
          <div className="mt-3 space-y-1.5">
            <div className="grid grid-cols-4 gap-0.5 rounded-md bg-muted/60 p-0.5">
              <FilterSegment
                active={categoryFilter === 'all'}
                label={t.promptLibrary.allFilter}
                onClick={() => setCategoryFilter('all')}
              >
                {t.promptLibrary.allFilter}
              </FilterSegment>
              {chatRuleCategories.map((category) => {
                const Icon = categoryIcon(category)
                return (
                  <FilterSegment
                    active={categoryFilter === category}
                    activeText={toneText[categoryToTone[category]]}
                    key={category}
                    label={categoryLabel(category, t)}
                    onClick={() => setCategoryFilter(category)}
                  >
                    <Icon className="icon-sm" />
                  </FilterSegment>
                )
              })}
            </div>
            <div className="grid grid-cols-4 gap-0.5 rounded-md bg-muted/60 p-0.5">
              <FilterSegment
                active={visibilityFilter === 'all'}
                label={t.promptLibrary.allFilter}
                onClick={() => setVisibilityFilter('all')}
              >
                {t.promptLibrary.allFilter}
              </FilterSegment>
              <FilterSegment
                active={visibilityFilter === 'chat'}
                label={t.promptLibrary.chatVisible}
                onClick={() => setVisibilityFilter('chat')}
              >
                <MessagesSquare className="icon-sm" />
              </FilterSegment>
              <FilterSegment
                active={visibilityFilter === 'editor'}
                label={t.promptLibrary.editorVisible}
                onClick={() => setVisibilityFilter('editor')}
              >
                <FileText className="icon-sm" />
              </FilterSegment>
              <FilterSegment
                active={visibilityFilter === 'hidden'}
                label={t.promptLibrary.hiddenEverywhere}
                onClick={() => setVisibilityFilter('hidden')}
              >
                <EyeOff className="icon-sm" />
              </FilterSegment>
            </div>
          </div>
        </div>
        <ScrollArea className="min-h-0 flex-1">
          <div className="space-y-3 p-2">
            {groupedRules.length > 0 ? groupedRules.map((group) => (
              <div key={group.category}>
                <div className="t-caption mb-1.5 flex items-center gap-2 px-2 text-muted-foreground">
                  <span className={cn('size-1.5 shrink-0 rounded-full', toneBar[categoryToTone[group.category]])} />
                  <span>{categoryLabel(group.category, t)}</span>
                  <span className="ml-auto shrink-0 tabular-nums text-muted-foreground/60">{group.rules.length}</span>
                </div>
                <div className="space-y-1">
                  {group.rules.map((rule) => (
                    <PromptListItem
                      isSelected={draft.selectedRuleId === rule.id}
                      key={rule.id}
                      onSelect={() => guardedLoad(rule)}
                      rule={rule}
                    />
                  ))}
                </div>
              </div>
            )) : (
              <div className="t-meta rounded-md border border-dashed border-border p-4 text-center text-muted-foreground">
                {t.promptLibrary.noPrompts}
              </div>
            )}
          </div>
        </ScrollArea>
      </aside>

      <ScrollArea className={cn('min-h-0 lg:block', isMobileDetailOpen ? 'block' : 'hidden')}>
        <div className="mx-auto flex max-w-6xl flex-col gap-4 p-4 md:p-6">
          <section className="space-y-4">
            <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border pb-3">
              <div className="flex min-w-0 items-center gap-2">
                <Button
                  aria-label={t.common.back}
                  className="size-8 lg:hidden"
                  onClick={() => setIsMobileDetailOpen(false)}
                  size="icon"
                  type="button"
                  variant="ghost"
                >
                  <ChevronLeft className="size-4" />
                </Button>
                <div className="min-w-0">
                <h2 className="t-section truncate text-foreground">
                  {selectedRule?.title
                    ?? (sourceUnavailable ? draft.title : t.promptLibrary.newPrompt)}
                </h2>
                {draft.isDirty ? (
                  <p className="t-meta text-warning">{t.promptLibrary.unsaved}</p>
                ) : null}
                {selectedRule?.access?.mode === 'shared' ? (
                  <p className="t-meta text-muted-foreground">
                    {selectedRule.access.permission === 'edit'
                      ? t.sharing.sharedCanEdit
                    : t.sharing.sharedViewOnly}
                  </p>
                ) : null}
                </div>
              </div>
              <div className="flex shrink-0 items-center gap-2">
                {sharing
                  && selectedRule?.serverTemplateId
                  && selectedRule.access?.mode !== 'shared' ? (
                  <Button
                    className="gap-1.5"
                    onClick={() => sharing.onShareRule(selectedRule)}
                    size="sm"
                    type="button"
                    variant="ghost"
                  >
                    <Users className="size-4" />
                    {t.sharing.share}
                  </Button>
                ) : null}
                <Button
                  className="gap-1.5 text-destructive hover:text-destructive"
                  disabled={!selectedRule || !canDeleteRule(selectedRule)}
                  onClick={() => void deletePrompt()}
                  size="sm"
                  type="button"
                  variant="ghost"
                >
                  <Trash2 className="size-4" />
                  {t.promptLibrary.deletePrompt}
                </Button>
                <Button
                  className="gap-1.5 bg-brand text-brand-foreground hover:bg-brand/90"
                  disabled={!canSaveDraft || isSaving}
                  onClick={() => void savePrompt()}
                  size="sm"
                  type="button"
                >
                  <Save className="size-4" />
                  {t.promptLibrary.savePrompt}
                </Button>
              </div>
            </div>

            <PromptUsageHint draft={draft} />

            {(sourceUnavailable || remoteConflict || permissionDowngraded) ? (
              <div className="rounded-md border border-warning/25 bg-warning-subtle px-3 py-2.5">
                <p className="t-label text-warning">
                  {sourceUnavailable
                    ? t.promptLibrary.sourceUnavailableTitle
                    : permissionDowngraded
                      ? t.promptLibrary.permissionDowngraded
                      : t.promptLibrary.remoteConflict}
                </p>
                <p className="mt-1 t-meta text-muted-foreground">
                  {sourceUnavailable
                    ? t.promptLibrary.sourceUnavailable
                    : permissionDowngraded
                      ? t.promptLibrary.permissionDowngradedHint
                      : t.promptLibrary.syncConflict}
                </p>
                <div className="mt-2 flex flex-wrap gap-2">
                  <Button onClick={keepPromptAsCopy} size="sm" type="button" variant="outline">
                    {t.promptLibrary.keepAsCopy}
                  </Button>
                  <Button onClick={discardUnavailableDraft} size="sm" type="button" variant="ghost">
                    {t.promptLibrary.discardDraft}
                  </Button>
                </div>
              </div>
            ) : null}

            <div className="space-y-4">
              <div className="grid gap-3 sm:grid-cols-2">
                <label className="block space-y-1.5">
                  <span className="t-label text-muted-foreground">{t.promptLibrary.labelLabel}</span>
                  <div className="flex items-center rounded-md border border-border bg-background px-2 focus-within:ring-2 focus-within:ring-ring">
                    <span className="text-sm font-semibold text-muted-foreground">@rules:</span>
                    <input
                      className="min-w-0 flex-1 border-0 bg-transparent px-1 py-2 text-sm font-semibold text-foreground outline-none"
                      maxLength={48}
                      onChange={(event) => updateDraft({ label: normalizeRuleLabel(event.target.value) })}
                      placeholder={t.promptLibrary.labelPlaceholder}
                      value={draft.label}
                    />
                  </div>
                </label>
                <label className="block space-y-1.5">
                  <span className="t-label text-muted-foreground">{t.promptLibrary.titleLabel}</span>
                  <input
                    className="h-10 w-full rounded-md border border-border bg-background px-3 text-sm text-foreground outline-none focus-visible:ring-2 focus-visible:ring-ring"
                    onChange={(event) => updateDraft({ title: event.target.value })}
                    placeholder={t.promptLibrary.titlePlaceholder}
                    value={draft.title}
                  />
                </label>
              </div>

              <div className="space-y-1.5">
                <span className="t-label text-muted-foreground">{t.promptLibrary.categoryLabel}</span>
                <div className="grid gap-2 sm:grid-cols-3">
                  {chatRuleCategories.map((category) => {
                    const Icon = categoryIcon(category)
                    const active = draft.category === category
                    const tone = categoryToTone[category]
                    return (
                      <button
                        aria-pressed={active}
                        className={cn(
                          'flex min-w-0 items-start gap-2 rounded-md border px-3 py-2 text-left transition-colors',
                          active ? toneActiveCard[tone] : 'border-border bg-background hover:bg-accent',
                        )}
                        key={category}
                        onClick={() => updateDraft({ category })}
                        type="button"
                      >
                        <Icon className={cn('mt-0.5 size-4 shrink-0', active ? toneText[tone] : 'text-muted-foreground')} />
                        <span className="min-w-0">
                          <span className="t-list block truncate">{categoryLabel(category, t)}</span>
                          <span className="t-meta-sm block truncate text-muted-foreground">
                            {categoryHint(category, t)}
                          </span>
                        </span>
                      </button>
                    )
                  })}
                </div>
              </div>

              <VisibilityPanel draft={draft} onVisibilityChange={setVisibility} />

              <div className="space-y-1.5">
                <div className={cn(
                  'grid gap-4',
                  draft.category === 'context' && 'lg:grid-cols-[minmax(0,1fr)_320px]',
                )}>
                  <div className="overflow-hidden rounded-md border border-border bg-background shadow-[0_1px_2px_var(--shadow-hairline)] focus-within:border-brand/40 focus-within:ring-2 focus-within:ring-brand/15">
                    <div className="flex items-center gap-2 border-b border-border bg-surface/50 px-3 py-1.5">
                      <span className="t-label text-muted-foreground">{t.promptLibrary.promptLabel}</span>
                      {draft.category === 'context' ? (
                        <span className="t-mono inline-flex items-center gap-1 rounded border border-warning/25 bg-warning-subtle/60 px-1.5 py-0.5 text-warning">
                          {contextPackPlaceholder}
                        </span>
                      ) : null}
                      <span className="t-hint ml-auto shrink-0 tabular-nums text-muted-foreground">
                        {countLabel(
                          draft.contentMarkdown.length,
                          t.promptLibrary.charCountOne,
                          t.promptLibrary.charCount,
                          draft.contentMarkdown.length.toLocaleString(locale),
                        )}
                      </span>
                      <TextImproveButton
                        disabled={!draft.contentMarkdown.trim() || !textImprovement.enabled}
                        isLoading={promptTextImprove.isImproving}
                        label={t.textImprove.improve}
                        loadingLabel={t.textImprove.improving}
                        onClick={() => void improvePrompt()}
                        reduceMotion={reduceMotion}
                      />
                    </div>
                    <div className="relative min-w-0">
                      <Textarea
                        aria-label={t.promptLibrary.promptLabel}
                        className="t-body min-h-[23rem] resize-y rounded-none border-0 bg-transparent shadow-none focus-visible:ring-0 [scrollbar-width:thin]"
                        id="prompt-library-prompt"
                        onChange={(event) => updateDraft({ contentMarkdown: event.target.value })}
                        placeholder={draft.category === 'context'
                          ? t.promptLibrary.contextPromptPlaceholder.replace('{placeholder}', contextPackPlaceholder)
                          : t.promptLibrary.promptPlaceholder}
                        value={draft.contentMarkdown}
                      />
                      <TextImproveFieldLayer
                        labels={{
                          accept: t.textImprove.accept,
                          changes: t.textImprove.changes,
                          noChanges: t.textImprove.noChanges,
                          reject: t.textImprove.reject,
                          title: t.textImprove.title,
                          warnings: t.textImprove.warnings,
                        }}
                        onAccept={acceptPromptImprovement}
                        onReject={promptTextImprove.clearProposal}
                        proposal={promptTextImprove.proposal}
                        reduceMotion={reduceMotion}
                      />
                    </div>
                  </div>

                  {draft.category === 'context' ? (
                    <ContextPackPanel
                      contextChips={contextChips}
                      contextOptions={contextOptions}
                      contextOptionByKey={contextOptionByKey}
                      contextQuery={contextQuery}
                      draft={draft}
                      onOpenPreview={() => setPreviewOpen(true)}
                      onQueryChange={setContextQuery}
                      onRemove={removeContextRef}
                      onToggle={toggleContextRef}
                    />
                  ) : null}
                </div>
                {promptImproveError ? (
                  <p className="t-meta text-warning">{promptImproveError}</p>
                ) : null}
              </div>
            </div>
            {draft.error ? (
              <p className="t-meta pt-3 text-warning">{draft.error}</p>
            ) : null}
          </section>
        </div>
      </ScrollArea>
      {isPreviewOpen ? (
        <RenderedPreviewModal onClose={() => setPreviewOpen(false)} preview={preview} title={t.promptLibrary.previewLabel} />
      ) : null}
      {pendingNav ? (
        <UnsavedGuardModal
          saveDisabled={!canSaveDraft}
          onCancel={() => setPendingNav(null)}
          onDiscard={() => {
            const run = pendingNav
            setPendingNav(null)
            run?.()
          }}
          onSave={() => {
            void (async () => {
              const ok = await savePrompt()
              const run = pendingNav
              setPendingNav(null)
              if (ok) run?.()
            })()
          }}
        />
      ) : null}
      </div>
    </div>
  )
}

function PromptUsageHint({ draft }: { draft: PromptDraft }) {
  const { t } = useLocale()
  const tone = categoryToTone[draft.category]
  const Icon = categoryIcon(draft.category)
  const shortcutLabel = normalizeRuleLabel(draft.label) || t.promptLibrary.labelPlaceholder
  const shortcut = `@rules:${shortcutLabel}`
  const usage = categoryUsage(draft.category, t)
    .replace('{mention}', shortcut)
    .replace('{placeholder}', contextPackPlaceholder)

  return (
    <div className="min-w-0 space-y-0.5">
      <p className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-0.5">
        <Icon aria-hidden className={cn('icon-xs shrink-0', toneText[tone])} />
        <span className="t-label text-foreground">{categoryLabel(draft.category, t)}</span>
        <span className="t-mono text-muted-foreground">{shortcut}</span>
      </p>
      <p className="t-meta text-muted-foreground">{usage}</p>
    </div>
  )
}

function VisibilityPanel({
  draft,
  onVisibilityChange,
}: {
  draft: PromptDraft
  onVisibilityChange: (key: keyof ChatRuleVisibility, value: boolean) => void
}) {
  const { t } = useLocale()
  const hidden = !draft.visibility.chat && !draft.visibility.editor

  return (
    <section className="flex flex-wrap items-center justify-between gap-x-3 gap-y-2">
      <div className="min-w-0">
        <span className="t-label text-foreground">{t.promptLibrary.visibilityLabel}</span>
        {hidden ? (
          <span className="t-meta-sm ml-2 inline-flex items-center gap-1 align-middle text-destructive/80">
            <EyeOff className="size-3" />
            {t.promptLibrary.hiddenEverywhere}
          </span>
        ) : null}
      </div>
      <div className="flex shrink-0 items-center gap-1.5">
        <VisChip
          active={draft.visibility.chat}
          icon={MessagesSquare}
          label={t.promptLibrary.chatVisible}
          onClick={() => onVisibilityChange('chat', !draft.visibility.chat)}
        />
        <VisChip
          active={draft.visibility.editor}
          icon={FileText}
          label={t.promptLibrary.editorVisible}
          onClick={() => onVisibilityChange('editor', !draft.visibility.editor)}
        />
        <VisChip
          active={draft.visibility.agent}
          icon={Sparkles}
          label={t.promptLibrary.agentVisible}
          onClick={() => onVisibilityChange('agent', !draft.visibility.agent)}
        />
      </div>
    </section>
  )
}

function VisChip({
  active,
  icon: Icon,
  label,
  onClick,
}: {
  active: boolean
  icon: LucideIcon
  label: string
  onClick: () => void
}) {
  return (
    <button
      aria-pressed={active}
      className={cn(
        'inline-flex h-7 items-center gap-1.5 rounded-md border px-2 text-xs font-medium transition-colors',
        active
          ? 'border-brand/40 bg-brand-subtle text-brand'
          : 'border-border text-muted-foreground hover:bg-accent hover:text-foreground',
      )}
      onClick={onClick}
      type="button"
    >
      <Icon className="size-3.5" />
      {label}
      {active ? <Check className="size-3" /> : null}
    </button>
  )
}

function FilterSegment({
  active,
  activeText,
  children,
  label,
  onClick,
}: {
  active: boolean
  activeText?: string
  children: ReactNode
  label: string
  onClick: () => void
}) {
  return (
    <button
      aria-label={label}
      aria-pressed={active}
      className={cn(
        'flex h-7 items-center justify-center rounded-[5px] px-1 text-xs font-medium transition-colors',
        active ? cn('bg-background shadow-sm', activeText ?? 'text-foreground') : 'text-muted-foreground hover:text-foreground',
      )}
      onClick={onClick}
      title={label}
      type="button"
    >
      {children}
    </button>
  )
}

function ContextPackPanel({
  contextChips,
  contextOptionByKey,
  contextOptions,
  contextQuery,
  draft,
  onOpenPreview,
  onQueryChange,
  onRemove,
  onToggle,
}: {
  contextChips: ReturnType<typeof chatAttachmentChipsFromRefs>
  contextOptionByKey: Map<string, ContextPickerOption>
  contextOptions: ContextPickerOption[]
  contextQuery: string
  draft: PromptDraft
  onOpenPreview: () => void
  onQueryChange: (value: string) => void
  onRemove: (ref: ChatContextReferenceRecord) => void
  onToggle: (ref: ChatContextReferenceRecord) => void
}) {
  const { t } = useLocale()
  const selectedKeys = new Set(draft.linkedContextRefs.map(chatContextRefKey))
  const selectedChips = contextChips.map((chip) => ({
    ...chip,
    option: contextOptionByKey.get(chatContextRefKey(chip.ref)),
  }))
  const trimmedQuery = contextQuery.trim().toLowerCase()
  const matchingOptions = contextOptions
    .filter((option) => !selectedKeys.has(chatContextRefKey(option.ref)))
    .filter((option) => {
      if (!trimmedQuery) return true
      return `${option.label} ${option.title}`.toLowerCase().includes(trimmedQuery)
    })
  const visibleOptions = matchingOptions.slice(0, contextPickerLimit)

  return (
    <section className="flex h-full min-h-0 flex-col overflow-hidden rounded-md border border-border bg-surface/35">
      <div className="border-b border-border/60 px-3 py-2.5">
        <div className="flex items-center justify-between gap-2">
          <h3 className="t-label text-foreground">{t.promptLibrary.contextLabel}</h3>
          <Badge className="t-hint h-5 rounded-full border-border/70 bg-background px-1.5" variant="outline">
            {draft.linkedContextRefs.length}
          </Badge>
        </div>
        <p className="t-meta-sm mt-1 text-muted-foreground">
          {t.promptLibrary.contextPickerHint}
        </p>
      </div>

      <div className="flex min-h-0 flex-1 flex-col gap-2 p-3">
        {selectedChips.length > 0 ? (
          <div className="space-y-1">
            {selectedChips.map((chip) => {
              const Icon = chip.option?.icon ?? Paperclip
              return (
                <div
                  className="flex min-w-0 items-center gap-2 rounded-md border border-border/70 bg-background/70 px-2 py-1.5"
                  key={chatContextRefKey(chip.ref)}
                >
                  <Icon className="size-4 shrink-0 text-muted-foreground" />
                  <span className="min-w-0 flex-1">
                    <span className="t-label block truncate text-foreground">{chip.label}</span>
                    <span className="t-meta-sm block truncate text-muted-foreground">{chip.title}</span>
                  </span>
                  <button
                    aria-label={t.promptLibrary.removeContext}
                    className="grid size-6 shrink-0 place-items-center rounded-md text-muted-foreground hover:bg-background hover:text-foreground"
                    onClick={() => onRemove(chip.ref)}
                    type="button"
                  >
                    <X className="size-3.5" />
                  </button>
                </div>
              )
            })}
          </div>
        ) : (
          <p className="t-meta rounded-md bg-background/55 px-2.5 py-2 text-muted-foreground">
            {t.promptLibrary.noContextSelected}
          </p>
        )}

        <label className="flex h-8 items-center gap-2 rounded-md border border-border bg-background px-2 focus-within:ring-2 focus-within:ring-ring">
          <Search className="size-4 shrink-0 text-muted-foreground" />
          <input
            className="min-w-0 flex-1 border-0 bg-transparent text-sm text-foreground outline-none"
            onChange={(event) => onQueryChange(event.target.value)}
            placeholder={t.promptLibrary.contextSearchPlaceholder}
            value={contextQuery}
          />
        </label>

        <div className="min-h-0 space-y-1">
          {visibleOptions.map((option) => (
            <ContextOptionButton
              isSelected={selectedKeys.has(chatContextRefKey(option.ref))}
              key={option.key}
              option={option}
              onClick={() => onToggle(option.ref)}
            />
          ))}
          {contextOptions.length === 0 ? (
            <p className="t-meta rounded-md bg-background/45 px-2.5 py-2 text-muted-foreground">
              {t.promptLibrary.noContextFiles}
            </p>
          ) : null}
          {contextOptions.length > 0 && visibleOptions.length === 0 ? (
            <p className="t-meta rounded-md bg-background/45 px-2.5 py-2 text-muted-foreground">
              {t.promptLibrary.noContextMatches}
            </p>
          ) : null}
          {matchingOptions.length > visibleOptions.length ? (
            <p className="t-meta-sm px-1 text-muted-foreground">
              {t.promptLibrary.contextResultsLimited.replace('{count}', String(visibleOptions.length))}
            </p>
          ) : null}
        </div>

        <Button className="mt-auto w-full gap-1.5" onClick={onOpenPreview} size="sm" type="button" variant="outline">
          <BookOpen className="size-4" />
          {t.promptLibrary.openPreview}
        </Button>
      </div>
    </section>
  )
}

function ContextOptionButton({
  isSelected,
  onClick,
  option,
}: {
  isSelected: boolean
  onClick: () => void
  option: ContextPickerOption
}) {
  const Icon = option.icon
  return (
    <button
      aria-pressed={isSelected}
      className={cn(
        'flex w-full min-w-0 items-center gap-2 rounded-md border border-transparent px-2 py-1.5 text-left transition-colors hover:bg-background/75',
        isSelected && 'border-brand/25 bg-brand-subtle text-brand',
      )}
      onClick={onClick}
      type="button"
    >
      <Icon className="size-4 shrink-0 text-muted-foreground" />
      <span className="min-w-0 flex-1">
        <span className="t-label block truncate">{option.label}</span>
        <span className="t-meta-sm block truncate text-muted-foreground">{option.title}</span>
      </span>
      {isSelected ? <Check className="size-4 shrink-0" /> : null}
    </button>
  )
}

function PromptListItem({
  isSelected,
  onSelect,
  rule,
}: {
  isSelected: boolean
  onSelect: () => void
  rule: ChatRuleRecord
}) {
  const { t } = useLocale()
  const normalized = normalizeChatRule(rule)
  const category = normalized.category ?? 'instruction'
  const tone = categoryToTone[category]
  const Icon = categoryIcon(category)
  const visibility = normalized.visibility ?? { agent: false, chat: true, editor: true }
  const isAutocompleteVisible = normalized.includeInAutocomplete !== false
  const hidden = !isAutocompleteVisible || (!visibility.chat && !visibility.editor)
  return (
    <button
      className={cn(
        'group flex w-full min-w-0 flex-col gap-0.5 border-l-2 py-1.5 pl-3 pr-2 text-left transition-colors',
        isSelected ? cn(toneAccentBorderLeft[tone], 'bg-card') : 'border-l-transparent hover:bg-card/60',
        hidden && 'opacity-60',
      )}
      onClick={onSelect}
      type="button"
    >
      <span className="flex min-w-0 items-center gap-2">
        <Icon aria-hidden className={cn('icon-sm shrink-0', hidden ? 'text-muted-foreground/60' : toneText[tone])} />
        <span className={cn(
          't-list min-w-0 flex-1 truncate',
          isSelected ? 'text-foreground' : 'text-foreground/90',
        )}>
          {normalized.title}
        </span>
      </span>
      <span className="t-meta-sm flex min-w-0 items-center gap-2 pl-[1.375rem] text-muted-foreground">
        <span className="t-mono min-w-0 flex-1 truncate text-muted-foreground">@rules:{normalized.label}</span>
        {hidden ? (
          <span className="inline-flex shrink-0 items-center gap-1 text-destructive/80">
            <EyeOff aria-hidden className="icon-xs shrink-0" />
            {t.promptLibrary.hiddenEverywhere}
          </span>
        ) : (
          <span className="inline-flex shrink-0 items-center gap-1.5 text-muted-foreground/70">
            {visibility.chat ? <MessagesSquare aria-label={t.promptLibrary.chatVisible} className="icon-xs" /> : null}
            {visibility.editor ? <FileText aria-label={t.promptLibrary.editorVisible} className="icon-xs" /> : null}
          </span>
        )}
      </span>
    </button>
  )
}

function RenderedPreviewModal({
  onClose,
  preview,
  title,
}: {
  onClose: () => void
  preview: string
  title: string
}) {
  const { t } = useLocale()
  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-background/75 px-4 py-8 backdrop-blur"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose()
      }}
    >
      <section
        aria-modal="true"
        className="w-full max-w-4xl overflow-hidden rounded-lg border border-border bg-background shadow-xl"
        role="dialog"
      >
        <div className="flex items-center justify-between gap-3 border-b border-border px-4 py-3">
          <div className="min-w-0">
            <h2 className="t-section truncate text-foreground">{title}</h2>
            <p className="t-meta text-muted-foreground">{t.promptLibrary.previewHint}</p>
          </div>
          <button
            aria-label={t.common.close}
            className="grid size-7 shrink-0 place-items-center rounded-md text-muted-foreground hover:bg-accent hover:text-foreground"
            onClick={onClose}
            type="button"
          >
            <X className="size-4" />
          </button>
        </div>
        <div className="max-h-[72svh] overflow-auto p-4">
          <pre className="whitespace-pre-wrap rounded-md bg-muted/45 p-4 text-xs leading-5 text-foreground">
            {preview}
          </pre>
        </div>
      </section>
    </div>
  )
}

function UnsavedGuardModal({
  onCancel,
  onDiscard,
  onSave,
  saveDisabled,
}: {
  onCancel: () => void
  onDiscard: () => void
  onSave: () => void
  saveDisabled: boolean
}) {
  const { t } = useLocale()
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-background/75 px-4 backdrop-blur"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onCancel()
      }}
    >
      <section
        aria-modal="true"
        className="w-full max-w-md overflow-hidden rounded-lg border border-border bg-background shadow-xl"
        role="dialog"
      >
        <div className="flex items-start gap-3 border-b border-border px-4 py-3">
          <span className="mt-0.5 grid size-7 shrink-0 place-items-center rounded-md bg-warning-subtle text-warning">
            <AlertTriangle className="size-4" />
          </span>
          <div className="min-w-0">
            <h2 className="t-section text-foreground">{t.promptLibrary.discardTitle}</h2>
            <p className="t-meta mt-0.5 text-muted-foreground">{t.promptLibrary.discardBody}</p>
          </div>
        </div>
        <div className="flex flex-wrap items-center justify-end gap-2 px-4 py-3">
          <Button autoFocus onClick={onCancel} type="button" variant="ghost">
            {t.promptLibrary.keepEditing}
          </Button>
          <Button
            className="text-destructive hover:text-destructive"
            onClick={onDiscard}
            type="button"
            variant="ghost"
          >
            {t.promptLibrary.discard}
          </Button>
          <Button
            className="gap-1.5 bg-brand text-brand-foreground hover:bg-brand/90"
            disabled={saveDisabled}
            onClick={onSave}
            size="sm"
            type="button"
          >
            <Save className="size-4" />
            {t.promptLibrary.savePrompt}
          </Button>
        </div>
      </section>
    </div>
  )
}

function draftFromRule(rule: ChatRuleRecord | null): PromptDraft {
  if (!rule) return emptyDraft
  const normalized = normalizeChatRule(rule)
  return {
    baseRevision: normalized.serverRevision ?? null,
    category: normalized.category ?? 'instruction',
    contentMarkdown: normalized.contentMarkdown,
    error: null,
    includeInAutocomplete: normalized.includeInAutocomplete ?? true,
    isDirty: false,
    label: normalized.label,
    linkedContextRefs: normalized.linkedContextRefs ?? [],
    selectedRuleId: normalized.id,
    sourceTemplateId: normalized.serverTemplateId ?? null,
    title: normalized.title,
    visibility: normalized.visibility ?? { agent: false, chat: true, editor: true },
  }
}

function draftRuleRecord(draft: PromptDraft, selectedRule: ChatRuleRecord | null): ChatRuleRecord {
  const now = new Date().toISOString()
  return normalizeChatRule({
    category: draft.category,
    contentMarkdown: draft.contentMarkdown,
    createdAt: selectedRule?.createdAt ?? now,
    id: draft.selectedRuleId ?? 'prompt-preview',
    includeInAutocomplete: draft.includeInAutocomplete,
    label: draft.label || 'preview',
    linkedContextRefs: draft.category === 'context' ? draft.linkedContextRefs : [],
    title: draft.title || draft.label || 'Preview',
    updatedAt: selectedRule?.updatedAt ?? now,
    visibility: draft.visibility,
  })
}

function categoryIcon(category: ChatRuleCategory) {
  if (category === 'function') return ListOrdered
  if (category === 'context') return BookOpen
  return Bot
}

function categoryLabel(category: ChatRuleCategory, t: ReturnType<typeof useLocale>['t']) {
  if (category === 'function') return t.promptLibrary.categoryFunction
  if (category === 'context') return t.promptLibrary.categoryContext
  return t.promptLibrary.categoryInstruction
}

function categoryHint(category: ChatRuleCategory, t: ReturnType<typeof useLocale>['t']) {
  if (category === 'function') return t.promptLibrary.categoryFunctionHint
  if (category === 'context') return t.promptLibrary.categoryContextHint
  return t.promptLibrary.categoryInstructionHint
}

function categoryUsage(category: ChatRuleCategory, t: ReturnType<typeof useLocale>['t']) {
  if (category === 'function') return t.promptLibrary.categoryFunctionUsage
  if (category === 'context') return t.promptLibrary.categoryContextUsage
  return t.promptLibrary.categoryInstructionUsage
}

function promptImprovementGuidance(draft: PromptDraft, t: ReturnType<typeof useLocale>['t']) {
  const label = normalizeRuleLabel(draft.label) || 'draft-label'
  const category = promptImprovementCategory(draft.category)
  const usage = categoryUsage(draft.category, t)
    .replace('{mention}', `@rules:${label}`)
    .replace('{placeholder}', contextPackPlaceholder)

  return [
    `Prompt Library category: ${category.name}.`,
    `Shortcut label: @rules:${label}.`,
    category.guidance,
    `Current UI usage guidance: ${usage}`,
    'Do not include this guidance in the improved prompt. Return only the improved prompt content.',
  ].join('\n')
}

function promptImprovementCategory(category: ChatRuleCategory) {
  if (category === 'function') {
    return {
      guidance: 'This prompt is a Function: it performs a reusable transformation such as translation, summarization, extraction, or rewriting. Optimize for clear inputs, task boundaries, chain-step reuse, and predictable output behavior.',
      name: 'Function',
    }
  }
  if (category === 'context') {
    return {
      guidance: `This prompt is a Context Pack: it provides long reusable context and may include rendered database file blocks. Preserve ${contextPackPlaceholder} exactly if it is present. If a file insertion point would improve the prompt, use ${contextPackPlaceholder} exactly once.`,
      name: 'Context Pack',
    }
  }
  return {
    guidance: 'This prompt is an Instruction: it changes the model role, behavior, style, working method, or skill posture. Optimize it as reusable model guidance without turning it into a one-off task.',
    name: 'Instruction',
  }
}

function messageFromUnknown(error: unknown) {
  return error instanceof Error ? error.message : String(error)
}

const categoryToTone: Record<ChatRuleCategory, MentionTone> = {
  context: 'warning',
  function: 'success',
  instruction: 'brand',
}
