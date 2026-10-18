import {
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type ChangeEvent,
  type FormEvent,
  type KeyboardEvent,
  type SyntheticEvent,
} from 'react'
import {
  Database,
  Info,
  Plus,
  SendHorizontal,
  SlidersHorizontal,
  Sparkles,
  X,
} from '@/components/icons'
import { Button } from '@/components/ui/button'
import { Chip } from '@/components/ui/chip'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { MentionMenu, type MentionMenuOption } from '@/components/ui/mention-menu'
import {
  OptionMenuHeader,
  OptionMenuItem,
  optionMenuContentClassName,
} from '@/components/ui/option-menu'
import { Textarea } from '@/components/ui/textarea'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { composerIconButtonClassName } from '@/features/composer/ComposerIconButton'
import { ComposerStopButton } from '@/features/composer/ComposerStopButton'
import { QuotaMeter } from '@/features/quota/QuotaMeter'
import { resizeTextareaToRows } from '@/features/composer/textareaAutosize'
import { useLocale } from '@/i18n/LocaleProvider'
import { cn } from '@/lib/utils'
import { detectCollectionMention, type CollectionMentionState } from '@/features/composer/collectionMention'
import { KnowledgeStatusMenu } from './KnowledgeStatusMenu'
import type { KnowledgeProfileOption } from './profileOptions'
import { profileDescription, profileDisplayName } from './stepLines'
import type { KnowledgeCollectionOption } from './types'
import { ComposerDisclosureHint } from '@/features/composer/ComposerDisclosureHint'

type KnowledgeComposerProps = {
  className?: string
  collections: KnowledgeCollectionOption[]
  connectedTop?: boolean
  defaultProfileId: string | null
  defaultTopK: number
  /** Hard ceiling for the final_k override field (capability `evidence_k_max`). */
  evidenceKMax: number
  /** Configured reranker provider id, shown in the run overview. */
  rerankerProvider: string | null
  disabled: boolean
  isReplacing?: boolean
  /** A previous ask is still running. Gates ONLY the send action (single-flight),
   * NOT the textarea/pickers — so the next question can be drafted meanwhile,
   * like the chat composer during generation. */
  running?: boolean
  notice: string | null
  /** Session-scoped draft question lifted to the shell so it survives a view switch. */
  draftQuestion: string
  onDraftQuestionChange: (question: string) => void
  onCancelReplace?: () => void
  onProfileChange: (profileId: string | null) => void
  onSelectedCollectionIdsChange: (ids: string[]) => void
  onStop: () => void
  onSubmit: (question: string) => void
  onTopKChange: (topK: number | null) => void
  onFinalKChange: (finalK: number | null) => void
  profileOptions: KnowledgeProfileOption[]
  selectedCollectionIds: string[]
  selectedProfileId: string | null
  topK: number | null
  finalK: number | null
}

/**
 * Bottom composer of the Ask mode: question textarea with an
 * `@`-triggered collection picker (the shared MentionMenu pattern),
 * removable collection chips, the manifest-driven profile picker and a
 * top_k options popover.
 */
export function KnowledgeComposer({
  className,
  collections,
  connectedTop = false,
  defaultProfileId,
  defaultTopK,
  evidenceKMax,
  rerankerProvider,
  disabled,
  isReplacing = false,
  running = false,
  notice,
  draftQuestion,
  onDraftQuestionChange,
  onCancelReplace,
  onProfileChange,
  onSelectedCollectionIdsChange,
  onStop,
  onSubmit,
  onTopKChange,
  onFinalKChange,
  profileOptions,
  selectedCollectionIds,
  selectedProfileId,
  topK,
  finalK,
}: KnowledgeComposerProps) {
  const { t } = useLocale()
  const textareaRef = useRef<HTMLTextAreaElement | null>(null)
  // Restore the session draft on mount (this composer unmounts on a view switch);
  // the sync effect mirrors edits and the submit-time clear back up to the shell.
  const [question, setQuestion] = useState(draftQuestion)
  const [mention, setMention] = useState<CollectionMentionState | null>(null)
  const [mentionIndex, setMentionIndex] = useState(0)
  useEffect(() => {
    setQuestion(draftQuestion)
    setMention(null)
  }, [draftQuestion])
  useEffect(() => {
    onDraftQuestionChange(question)
  }, [question, onDraftQuestionChange])

  useLayoutEffect(() => {
    resizeTextareaToRows(textareaRef.current, 5)
  }, [question])

  const selectedCollections = collections.filter((collection) =>
    selectedCollectionIds.includes(collection.id))
  // Collections not yet attached — the single source for both the typed-`@`
  // mention menu and the `+` picker dropdown (no duplicate filter).
  const addableCollections = useMemo(
    () => collections.filter((collection) => !selectedCollectionIds.includes(collection.id)),
    [collections, selectedCollectionIds],
  )
  const mentionCandidates = useMemo(() => {
    if (!mention) return []
    const query = mention.query.toLowerCase()
    return addableCollections.filter((collection) => collection.title.toLowerCase().includes(query))
  }, [addableCollections, mention])
  const mentionOptions: MentionMenuOption[] = mentionCandidates.map((collection) => ({
    group: t.knowledge.collectionGroup,
    icon: Database,
    isCategory: false,
    primary: collection.title,
    secondary: t.knowledge.collectionMenuHandle,
    tone: 'brand',
  }))

  // Effective retrieval breadth for the field placeholder + the run overview:
  // top_k falls back to the server default; final_k to the active profile's
  // factor (min round(top_k * factor), clamped to the evidence ceiling).
  const effectiveProfileId = selectedProfileId ?? defaultProfileId ?? profileOptions[0]?.id ?? null
  const effectiveProfile = profileOptions.find((option) => option.id === effectiveProfileId) ?? null
  const effectiveTopK = topK ?? defaultTopK
  const finalKFactor = effectiveProfile?.finalKFactor ?? 1
  const defaultFinalK = Math.max(1, Math.min(Math.round(effectiveTopK * finalKFactor), evidenceKMax))
  const effectiveFinalK = finalK ?? defaultFinalK

  const canSubmit = !disabled
    && !running
    && question.trim().length > 0
    && selectedCollectionIds.length > 0
  const sendLabel = isReplacing ? t.knowledge.updateQuestion : t.knowledge.send

  function updateMentionFromCaret(value: string, caret: number) {
    const nextMention = detectCollectionMention(value, caret)
    setMention(nextMention)
    setMentionIndex(0)
  }

  function updateMentionFromTextarea(textarea: HTMLTextAreaElement) {
    updateMentionFromCaret(textarea.value, textarea.selectionStart ?? textarea.value.length)
  }

  function refreshMentionAfterDomInput() {
    window.requestAnimationFrame(() => {
      const textarea = textareaRef.current
      if (!textarea || document.activeElement !== textarea) return
      updateMentionFromTextarea(textarea)
    })
  }

  function handleQuestionChange(event: ChangeEvent<HTMLTextAreaElement>) {
    setQuestion(event.currentTarget.value)
    updateMentionFromTextarea(event.currentTarget)
    refreshMentionAfterDomInput()
  }

  function handleQuestionCaretChange(event: SyntheticEvent<HTMLTextAreaElement>) {
    updateMentionFromTextarea(event.currentTarget)
  }

  function handleQuestionKeyUp(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === 'Escape' || event.key === 'Enter' || event.key === 'Tab') return
    if (mention && mentionOptions.length > 0 && (event.key === 'ArrowDown' || event.key === 'ArrowUp')) {
      return
    }
    updateMentionFromTextarea(event.currentTarget)
  }

  function selectMentionOption(index: number) {
    const collection = mentionCandidates[index]
    if (!collection || !mention) return
    const end = mention.start + 1 + mention.query.length
    const nextValue = `${question.slice(0, mention.start)}${question.slice(end)}`
    onSelectedCollectionIdsChange([...selectedCollectionIds, collection.id])
    setQuestion(nextValue)
    setMention(null)
    window.requestAnimationFrame(() => {
      const textarea = textareaRef.current
      if (!textarea) return
      textarea.focus()
      textarea.setSelectionRange(mention.start, mention.start)
    })
  }

  function handleKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (mention && mentionOptions.length > 0) {
      if (event.key === 'ArrowDown') {
        event.preventDefault()
        setMentionIndex((current) => (current + 1) % mentionOptions.length)
        return
      }
      if (event.key === 'ArrowUp') {
        event.preventDefault()
        setMentionIndex((current) => (current - 1 + mentionOptions.length) % mentionOptions.length)
        return
      }
      if (event.key === 'Enter' || event.key === 'Tab') {
        event.preventDefault()
        selectMentionOption(mentionIndex)
        return
      }
    }
    if (mention && event.key === 'Escape') {
      event.preventDefault()
      setMention(null)
      return
    }
    if (
      event.key === 'Enter'
      && !event.ctrlKey
      && !event.metaKey
      && !event.shiftKey
      && !event.nativeEvent.isComposing
    ) {
      event.preventDefault()
      submitQuestion()
    }
  }

  function submitQuestion() {
    if (!canSubmit) return
    onSubmit(question.trim())
    setQuestion('')
    setMention(null)
  }

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    submitQuestion()
  }

  function removeCollection(collectionId: string) {
    onSelectedCollectionIdsChange(selectedCollectionIds.filter((id) => id !== collectionId))
  }

  return (
    <form className={cn('mx-auto max-w-5xl', className)} onSubmit={handleSubmit}>
      <div
        className={cn(
          'relative overflow-visible rounded-xl border border-border bg-card px-2.5 py-2 shadow-[0_8px_28px_-12px_var(--shadow-soft)] transition-[border-color,box-shadow] duration-150 focus-within:border-brand/60 focus-within:ring-2 focus-within:ring-brand/15',
          connectedTop && 'rounded-t-none border-t-0 shadow-[0_18px_40px_-30px_var(--brand)]',
        )}
      >
        {mention && (
          <MentionMenu
            activeIndex={mentionIndex}
            labels={{
              backHint: t.chat.mentionBackHint,
              closeHint: t.chat.mentionCloseHint,
              filterPlaceholder: t.chat.mentionFilterPlaceholder,
              navHint: t.chat.mentionNavHint,
              rootTitle: t.knowledge.collectionPickerTitle,
              selectHint: t.chat.mentionSelectHint,
            }}
            onHover={setMentionIndex}
            onSelect={selectMentionOption}
            options={mentionOptions.length > 0
              ? mentionOptions
              : [{
                // A state without an action: picking it selects nothing.
                disabled: true,
                group: undefined,
                icon: Database,
                isCategory: false,
                state: true,
                primary: t.knowledge.noCollectionMatches,
                secondary: t.knowledge.collectionPickerHint,
                tone: 'brand',
              }]}
            scope={{ icon: Database, kind: t.knowledge.collections, query: mention.query, tone: 'brand' }}
          />
        )}

        {selectedCollections.length > 0 && (
          <div className="mb-1.5 flex flex-wrap items-center gap-1.5">
            {selectedCollections.map((collection) => (
              <Chip
                active
                aria-label={`${t.knowledge.removeCollection}: ${collection.title}`}
                dot="bg-brand"
                key={collection.id}
                onClick={() => removeCollection(collection.id)}
                title={t.knowledge.removeCollection}
              >
                <span className="max-w-48 truncate">{collection.title}</span>
                <X aria-hidden="true" className="size-3 shrink-0" />
              </Chip>
            ))}
          </div>
        )}

        {isReplacing && (
          <div className="mb-1.5 flex min-w-0 items-center justify-between gap-2 rounded-md border border-brand/20 bg-brand-subtle px-2 py-1">
            <span className="min-w-0 truncate t-meta-sm font-semibold text-brand">
              {t.knowledge.replacingQuestion}
            </span>
            {onCancelReplace && (
              <Tooltip>
                <TooltipTrigger asChild>
                  <Button
                    aria-label={t.knowledge.cancelEdit}
                    className="size-5 shrink-0 text-brand hover:bg-brand/10 hover:text-brand"
                    onClick={onCancelReplace}
                    size="icon"
                    type="button"
                    variant="ghost"
                  >
                    <X className="size-3" />
                  </Button>
                </TooltipTrigger>
                <TooltipContent>{t.knowledge.cancelEdit}</TooltipContent>
              </Tooltip>
            )}
          </div>
        )}

        <Textarea
          aria-label={t.knowledge.composerPlaceholder}
          className="min-h-16 resize-none border-0 bg-transparent pb-2 pl-2 pr-2 pt-2 text-sm font-normal leading-6 shadow-none placeholder:text-muted-foreground/70 focus-visible:ring-0"
          disabled={disabled}
          onBlur={() => setMention(null)}
          onChange={handleQuestionChange}
          onClick={handleQuestionCaretChange}
          onFocus={handleQuestionCaretChange}
          onKeyDown={handleKeyDown}
          onKeyUp={handleQuestionKeyUp}
          onSelect={handleQuestionCaretChange}
          placeholder={t.knowledge.composerPlaceholder}
          ref={textareaRef}
          rows={1}
          value={question}
        />

        <div className="mt-1.5 flex items-center justify-between gap-2 border-t border-border/70 pt-1.5">
          <div className="flex min-w-0 items-center gap-1 overflow-hidden">
            <DropdownMenu modal={false}>
              <Tooltip>
                <DropdownMenuTrigger asChild>
                  <TooltipTrigger asChild>
                    <Button
                      aria-label={t.knowledge.addCollection}
                      className={composerIconButtonClassName}
                      disabled={disabled || collections.length === 0}
                      type="button"
                      variant="ghost"
                    >
                      <Plus />
                    </Button>
                  </TooltipTrigger>
                </DropdownMenuTrigger>
                <TooltipContent>{t.knowledge.addCollection}</TooltipContent>
              </Tooltip>
              <DropdownMenuContent align="start" className={optionMenuContentClassName} side="top" sideOffset={8}>
                <OptionMenuHeader count={addableCollections.length} title={t.knowledge.collectionPickerTitle} />
                {addableCollections.length > 0 ? (
                  <div className="py-1">
                    {addableCollections.map((collection) => (
                      <OptionMenuItem
                        active={false}
                        description={t.knowledge.collectionMenuHandle}
                        icon={Database}
                        key={collection.id}
                        label={collection.title}
                        onSelect={() =>
                          onSelectedCollectionIdsChange([...selectedCollectionIds, collection.id])}
                      />
                    ))}
                  </div>
                ) : (
                  <p className="px-2.5 py-2 t-meta text-muted-foreground">{t.knowledge.allCollectionsAdded}</p>
                )}
              </DropdownMenuContent>
            </DropdownMenu>
            <KnowledgeProfileMenu
              defaultProfileId={defaultProfileId}
              disabled={disabled}
              onProfileChange={onProfileChange}
              profileOptions={profileOptions}
              selectedProfileId={selectedProfileId}
            />
            <DropdownMenu modal={false}>
              <Tooltip>
                <DropdownMenuTrigger asChild>
                  <TooltipTrigger asChild>
                    <Button
                      aria-label={t.knowledge.options}
                      className={composerIconButtonClassName}
                      disabled={disabled}
                      type="button"
                      variant="ghost"
                    >
                      <SlidersHorizontal />
                    </Button>
                  </TooltipTrigger>
                </DropdownMenuTrigger>
                <TooltipContent>{t.knowledge.options}</TooltipContent>
              </Tooltip>
              <DropdownMenuContent align="start" className={optionMenuContentClassName} side="top" sideOffset={8}>
                <OptionMenuHeader count={2} title={t.knowledge.options} />
                <div className="px-2.5 py-2">
                  <div className="flex items-center gap-1">
                    <label className="block t-label text-foreground" htmlFor="knowledge-top-k">
                      {t.knowledge.topKLabel}
                    </label>
                    <ParameterInfo body={t.knowledge.topKInfo} title={t.knowledge.topKLabel} />
                  </div>
                  <input
                    className="mt-1.5 h-8 w-full rounded-md border border-border bg-background px-2 text-sm tabular-nums text-foreground outline-none focus-visible:ring-1 focus-visible:ring-ring"
                    id="knowledge-top-k"
                    max={50}
                    min={1}
                    onChange={(event) => {
                      const raw = event.target.value.trim()
                      if (raw === '') {
                        onTopKChange(null)
                        return
                      }
                      const value = Number(raw)
                      if (Number.isFinite(value)) {
                        onTopKChange(Math.min(50, Math.max(1, Math.round(value))))
                      }
                    }}
                    placeholder={String(defaultTopK)}
                    type="number"
                    value={topK ?? ''}
                  />
                  <p className="mt-1 t-meta-sm text-muted-foreground">
                    {t.knowledge.topKHint.replace('{default}', String(defaultTopK))}
                  </p>
                </div>
                <DropdownMenuSeparator className="mx-0 my-0" />
                <div className="px-2.5 py-2">
                  <div className="flex items-center gap-1">
                    <label className="block t-label text-foreground" htmlFor="knowledge-final-k">
                      {t.knowledge.finalKLabel}
                    </label>
                    <ParameterInfo body={t.knowledge.finalKInfo} title={t.knowledge.finalKLabel} />
                  </div>
                  <input
                    className="mt-1.5 h-8 w-full rounded-md border border-border bg-background px-2 text-sm tabular-nums text-foreground outline-none focus-visible:ring-1 focus-visible:ring-ring"
                    id="knowledge-final-k"
                    max={evidenceKMax}
                    min={1}
                    onChange={(event) => {
                      const raw = event.target.value.trim()
                      if (raw === '') {
                        onFinalKChange(null)
                        return
                      }
                      const value = Number(raw)
                      if (Number.isFinite(value)) {
                        onFinalKChange(Math.min(evidenceKMax, Math.max(1, Math.round(value))))
                      }
                    }}
                    placeholder={String(defaultFinalK)}
                    type="number"
                    value={finalK ?? ''}
                  />
                  <p className="mt-1 t-meta-sm text-muted-foreground">
                    {t.knowledge.finalKHint.replace('{default}', String(defaultFinalK))}
                  </p>
                </div>
              </DropdownMenuContent>
            </DropdownMenu>
          </div>
          <div className="flex shrink-0 items-center gap-1">
            <QuotaMeter disabled={disabled} />
            <KnowledgeStatusMenu
              disabled={disabled}
              effectiveFinalK={effectiveFinalK}
              effectiveTopK={effectiveTopK}
              finalKOverridden={finalK != null}
              profile={effectiveProfile}
              rerankerProvider={rerankerProvider}
              topKOverridden={topK != null}
            />
            {running ? (
              <ComposerStopButton label={t.knowledge.stopAsk} onClick={onStop} />
            ) : (
              <Button
                aria-label={sendLabel}
                className={cn(
                  'size-7 shrink-0 rounded-md',
                  canSubmit
                    ? 'bg-brand text-brand-foreground hover:bg-brand/90 hover:text-brand-foreground'
                    : 'text-muted-foreground/45',
                )}
                disabled={!canSubmit}
                size="icon"
                type="submit"
                variant={canSubmit ? 'default' : 'ghost'}
              >
                <SendHorizontal className="size-4" />
              </Button>
            )}
          </div>
        </div>
      </div>
      {notice && (
        <p className="mt-1.5 px-1 t-meta text-destructive">{notice}</p>
      )}
      <ComposerDisclosureHint />
    </form>
  )
}

/** Info affordance next to a setting: an `Info` glyph that opens a small
 * hover-card explaining the parameter — the same hover-card treatment as the
 * model picker. */
function ParameterInfo({ body, title }: { body: string; title: string }) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span
          className="flex size-4 shrink-0 items-center justify-center rounded-full text-muted-foreground/45 hover:text-muted-foreground"
          role="img"
        >
          <Info className="size-3.5" />
        </span>
      </TooltipTrigger>
      <TooltipContent
        className="w-72 rounded-xl border border-border bg-card p-3 text-left shadow-lg"
        side="right"
        sideOffset={8}
      >
        <p className="t-meta-sm font-medium text-foreground">{title}</p>
        <p className="mt-1 t-meta-sm leading-relaxed text-muted-foreground">{body}</p>
      </TooltipContent>
    </Tooltip>
  )
}

function KnowledgeProfileMenu({
  defaultProfileId,
  disabled,
  onProfileChange,
  profileOptions,
  selectedProfileId,
}: {
  defaultProfileId: string | null
  disabled: boolean
  onProfileChange: (profileId: string | null) => void
  profileOptions: KnowledgeProfileOption[]
  selectedProfileId: string | null
}) {
  const { t } = useLocale()
  if (profileOptions.length === 0) return null

  const effectiveId = selectedProfileId ?? defaultProfileId ?? profileOptions[0]?.id
  const effectiveLabel = effectiveId ? profileDisplayName(effectiveId, t.knowledge) : ''
  const triggerLabel = `${t.knowledge.profile}: ${effectiveLabel}`

  return (
    <DropdownMenu modal={false}>
      <Tooltip>
        <DropdownMenuTrigger asChild>
          <TooltipTrigger asChild>
            <Button
              aria-label={triggerLabel}
              className={cn(composerIconButtonClassName, 'w-auto gap-1 px-1.5')}
              disabled={disabled}
              type="button"
              variant="ghost"
            >
              <Sparkles className="size-3.5 shrink-0" />
              <span className="max-w-24 truncate text-xs">{effectiveLabel}</span>
            </Button>
          </TooltipTrigger>
        </DropdownMenuTrigger>
        <TooltipContent>{triggerLabel}</TooltipContent>
      </Tooltip>
      <DropdownMenuContent align="start" className={optionMenuContentClassName} side="top" sideOffset={8}>
        <OptionMenuHeader
          count={profileOptions.length}
          title={t.knowledge.profile}
          value={effectiveLabel}
        />
        <div className="py-1">
          {profileOptions.map((option) => {
            const baseDescription = profileDescription(option.id, t.knowledge)
            const degradedHint = option.degraded.length > 0
              ? t.knowledge.profileDegradedHint.replace('{stages}', option.degraded.join(', '))
              : null
            const description = [baseDescription, degradedHint]
              .filter(Boolean)
              .join(' · ')
            return (
              <OptionMenuItem
                active={option.id === effectiveId}
                description={description || undefined}
                icon={Sparkles}
                key={option.id}
                label={profileDisplayName(option.id, t.knowledge)}
                onSelect={() => onProfileChange(option.id)}
              />
            )
          })}
        </div>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
