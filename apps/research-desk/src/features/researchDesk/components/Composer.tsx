import {
  AlertTriangle,
  Check,
  FileText,
  ListChecks,
  LoaderCircle,
  PanelBottomClose,
  Repeat2,
  Search,
  SendHorizontal,
  Shield,
  SlidersHorizontal,
  type LucideIcon,
} from '@/components/icons'
import {
  type Dispatch,
  forwardRef,
  useLayoutEffect,
  useRef,
  useState,
  type SetStateAction,
  type FormEvent,
  type KeyboardEvent,
} from 'react'
import { motion } from 'motion/react'
import { resizeTextareaToRows } from '@/features/composer/textareaAutosize'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { OptionMenuHeader, OptionMenuItem, optionMenuContentClassName } from '@/components/ui/option-menu'
import { StatusRow, SummaryGroup } from '@/components/ui/status-summary'
import { Separator } from '@/components/ui/separator'
import { Textarea } from '@/components/ui/textarea'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import {
  DEEP_RESEARCH_FIRST_ROUND_QUERIES,
  DEEP_RESEARCH_MAX_ROUNDS,
  type CreateResearchRunRequest,
} from '@/features/researchRuns/types'
import { ComposerIconButton, composerIconButtonClassName } from '@/features/composer/ComposerIconButton'
import { QuotaMeter } from '@/features/quota/QuotaMeter'
import { useLocale } from '@/i18n/LocaleProvider'
import { cn } from '@/lib/utils'
import { appMotion } from '@/motion/transitions'
import { ComposerDisclosureHint } from '@/features/composer/ComposerDisclosureHint'

type ComposerProps = {
  form: ComposerFormState
  isSubmitting?: boolean
  onHide: () => void
  onSubmit: (request: CreateResearchRunRequest) => Promise<boolean>
  reduceMotion: boolean | null
  selectedStack: string
  setForm: Dispatch<SetStateAction<ComposerFormState>>
  /** Externally disable submission (send button, Enter, form submit) —
   * e.g. while the auth session is still resolving. Typing stays enabled. */
  submitDisabled?: boolean
  submissionError?: string | null
}

export type ComposerFormState = {
  confidenceStop: 6 | 7 | 8 | 9
  firstRoundQueries: 4 | 6 | 8
  maxRounds: 1 | 2 | 3 | 4
  minRounds: 1 | 2
  question: string
  reportProfile: 'schnell' | 'compact' | 'deep'
}

type ComposerReportProfilePreset = Pick<
  ComposerFormState,
  'confidenceStop' | 'firstRoundQueries' | 'maxRounds' | 'minRounds' | 'reportProfile'
>

export const composerReportProfilePresets: Record<ComposerFormState['reportProfile'], ComposerReportProfilePreset> = {
  schnell: {
    confidenceStop: 6,
    firstRoundQueries: 6,
    maxRounds: 1,
    minRounds: 1,
    reportProfile: 'schnell',
  },
  compact: {
    confidenceStop: 7,
    firstRoundQueries: 6,
    maxRounds: 2,
    minRounds: 1,
    reportProfile: 'compact',
  },
  deep: {
    confidenceStop: 7,
    firstRoundQueries: DEEP_RESEARCH_FIRST_ROUND_QUERIES,
    maxRounds: DEEP_RESEARCH_MAX_ROUNDS,
    minRounds: 2,
    reportProfile: 'deep',
  },
}

export function applyComposerReportProfilePreset(
  currentForm: ComposerFormState,
  reportProfile: ComposerFormState['reportProfile'],
): ComposerFormState {
  return {
    ...currentForm,
    ...composerReportProfilePresets[reportProfile],
  }
}

export const defaultComposerFormState: ComposerFormState = {
  ...composerReportProfilePresets.deep,
  question: '',
}

type ComposerMenuKey =
  | 'confidence'
  | 'maxRounds'
  | 'more'
  | 'queries'
  | 'report'
  | 'summary'

type ComposerOption = {
  description: string
  label: string
  value: string
}

function switchComposerMenu(
  currentMenu: ComposerMenuKey | null,
  nextMenu: ComposerMenuKey,
  onOpenMenuChange: (menu: ComposerMenuKey | null) => void,
) {
  if (currentMenu === nextMenu) return

  if (currentMenu === null) {
    onOpenMenuChange(nextMenu)
    return
  }

  window.setTimeout(() => onOpenMenuChange(nextMenu), 0)
}

export function buildComposerRequest(
  form: ComposerFormState,
  question: string,
  selectedStack: string,
): CreateResearchRunRequest {
  // Send only what a human actually changed. The backend skips profile
  // application for every field the request states explicitly, so sending
  // all four unconditionally made the report profile decorative: the run
  // used the composer's values even where the profile disagreed.
  const preset = composerReportProfilePresets[form.reportProfile]
  const minRounds = Math.min(
    form.minRounds,
    form.maxRounds,
  ) as ComposerFormState['minRounds']
  const overrides: CreateResearchRunRequest['agentOverrides'] = {
    reportProfile: form.reportProfile,
  }
  if (form.confidenceStop !== preset.confidenceStop) {
    overrides.confidenceStop = form.confidenceStop
  }
  if (form.firstRoundQueries !== preset.firstRoundQueries) {
    overrides.firstRoundQueries = form.firstRoundQueries
  }
  if (form.maxRounds !== preset.maxRounds) {
    overrides.maxRounds = form.maxRounds
  }
  if (minRounds !== preset.minRounds) {
    overrides.minRounds = minRounds
  }
  return {
    agentOverrides: overrides,
    mode: 'research',
    question: question.trim(),
    stack: selectedStack,
  }
}

export async function runComposerSubmission({
  form,
  onSubmit,
  selectedStack,
  setForm,
}: {
  form: ComposerFormState
  onSubmit: (request: CreateResearchRunRequest) => Promise<boolean>
  selectedStack: string
  setForm: Dispatch<SetStateAction<ComposerFormState>>
}): Promise<boolean> {
  const submittedQuestion = form.question
  const accepted = await onSubmit(buildComposerRequest(form, submittedQuestion, selectedStack))
  if (!accepted) return false

  setForm((currentForm) => currentForm.question === submittedQuestion
    ? { ...currentForm, question: '' }
    : currentForm)
  return true
}

export function ResearchSubmissionAlert({ message }: { message: string }) {
  return (
    <div
      className="mt-2 flex items-start gap-1.5 rounded-md border border-destructive/25 bg-destructive/5 px-3 py-2 t-meta text-destructive"
      data-research-submission-error
      role="alert"
    >
      <AlertTriangle className="mt-0.5 size-3.5 shrink-0" />
      <span>{message}</span>
    </div>
  )
}

export const Composer = forwardRef<HTMLElement, ComposerProps>(function Composer(
  {
    form,
    isSubmitting = false,
    onHide,
    onSubmit,
    reduceMotion,
    selectedStack,
    setForm,
    submitDisabled,
    submissionError,
  },
  ref,
) {
  const { t } = useLocale()
  const questionTextareaRef = useRef<HTMLTextAreaElement | null>(null)
  const [openMenu, setOpenMenu] = useState<ComposerMenuKey | null>(null)
  const canSubmit = !submitDisabled && form.question.trim().length > 0
  const reportProfileOptions: ComposerOption[] = [
    {
      description: t.composer.optionSchnellDescription,
      label: t.composer.schnell,
      value: 'schnell',
    },
    {
      description: t.composer.optionCompactDescription,
      label: t.composer.compact,
      value: 'compact',
    },
    {
      description: t.composer.optionDeepDescription,
      label: t.composer.deep,
      value: 'deep',
    },
  ]
  const confidenceOptions: ComposerOption[] = [
    {
      description: t.composer.optionConfidence6Description,
      label: '6 / 10',
      value: '6',
    },
    {
      description: t.composer.optionConfidence7Description,
      label: '7 / 10',
      value: '7',
    },
    {
      description: t.composer.optionConfidence8Description,
      label: '8 / 10',
      value: '8',
    },
    {
      description: t.composer.optionConfidence9Description,
      label: '9 / 10',
      value: '9',
    },
  ]
  const firstQueryOptions: ComposerOption[] = [
    {
      description: t.composer.optionQueries4Description,
      label: '4',
      value: '4',
    },
    {
      description: t.composer.optionQueries6Description,
      label: '6',
      value: '6',
    },
    {
      description: t.composer.optionQueries8Description,
      label: '8',
      value: '8',
    },
  ]
  const maxRoundOptions: ComposerOption[] = [
    {
      description: t.composer.optionRounds1Description,
      label: '1',
      value: '1',
    },
    {
      description: t.composer.optionRounds2Description,
      label: '2',
      value: '2',
    },
    {
      description: t.composer.optionRounds3Description,
      label: '3',
      value: '3',
    },
    {
      description: t.composer.optionRounds4Description,
      label: '4',
      value: '4',
    },
  ]

  useLayoutEffect(() => {
    resizeTextareaToRows(questionTextareaRef.current, 4)
  }, [form.question])

  function submitResearch(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    void submitCurrentQuestion()
  }

  function handleQuestionKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (
      event.key !== 'Enter'
      || event.ctrlKey
      || event.metaKey
      || event.shiftKey
      || event.nativeEvent.isComposing
    ) {
      return
    }

    event.preventDefault()
    void submitCurrentQuestion()
  }

  async function submitCurrentQuestion() {
    if (!canSubmit) return
    const accepted = await runComposerSubmission({
      form,
      onSubmit,
      selectedStack,
      setForm,
    })
    if (!accepted) questionTextareaRef.current?.focus()
  }

  return (
    <motion.section
      id="research-composer"
      ref={ref}
      initial={reduceMotion ? false : { opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0 }}
      transition={appMotion.composer}
      className="shrink-0 px-4 pb-2 pt-2"
    >
      <form aria-busy={isSubmitting} className="mx-auto max-w-4xl" onSubmit={submitResearch}>
        <div className="relative rounded-xl border border-border bg-card px-3 py-2.5 shadow-[0_8px_28px_-12px_var(--shadow-soft)] transition-[border-color,box-shadow] duration-150 focus-within:border-brand/60 focus-within:ring-2 focus-within:ring-brand/15">
          <Textarea
            aria-label={t.composer.placeholder}
            className={cn(
              'min-h-16 resize-none border-0 bg-transparent pb-2 pl-2 pr-11 pt-2 text-sm font-normal leading-6 shadow-none placeholder:text-muted-foreground/70 focus-visible:ring-0',
              '[scrollbar-width:thin]',
              '[scrollbar-color:color-mix(in_oklch,var(--muted-foreground)_22%,transparent)_transparent]',
              '[&::-webkit-scrollbar]:w-1',
              '[&::-webkit-scrollbar-track]:bg-transparent',
              '[&::-webkit-scrollbar-thumb]:rounded-full',
              '[&::-webkit-scrollbar-thumb]:bg-border/70',
              'hover:[&::-webkit-scrollbar-thumb]:bg-muted-foreground/35',
            )}
            onChange={(event) => setForm((currentForm) => ({
              ...currentForm,
              question: event.target.value,
            }))}
            onKeyDown={handleQuestionKeyDown}
            placeholder={t.composer.placeholder}
            ref={questionTextareaRef}
            rows={1}
            value={form.question}
          />
          <div className="mt-1.5 flex items-center justify-between gap-2 border-t border-border/70 pt-1.5">
            <div className="flex min-w-0 items-center gap-1 overflow-hidden">
              <ComposerIconButton
                icon={PanelBottomClose}
                label={t.composer.hide}
                onClick={onHide}
              />
              <Separator className="mx-0.5 h-5" orientation="vertical" />
              <ComposerParameterMenu
                icon={FileText}
                label={t.composer.reportProfile}
                menuKey="report"
                onOpenMenuChange={setOpenMenu}
                onValueChange={(value) => setForm((currentForm) => applyComposerReportProfilePreset(
                  currentForm,
                  value as ComposerFormState['reportProfile'],
                ))}
                openMenu={openMenu}
                options={reportProfileOptions}
                showValue
                value={form.reportProfile}
              />
              <ComposerParameterMenu
                icon={Shield}
                label={t.composer.confidenceTarget}
                menuKey="confidence"
                onOpenMenuChange={setOpenMenu}
                onValueChange={(value) => setForm((currentForm) => ({
                  ...currentForm,
                  confidenceStop: Number(value) as ComposerFormState['confidenceStop'],
                }))}
                openMenu={openMenu}
                options={confidenceOptions}
                value={String(form.confidenceStop)}
              />
              <ComposerParameterMenu
                icon={Search}
                label={t.composer.firstQueries}
                menuKey="queries"
                onOpenMenuChange={setOpenMenu}
                onValueChange={(value) => setForm((currentForm) => ({
                  ...currentForm,
                  firstRoundQueries: Number(value) as ComposerFormState['firstRoundQueries'],
                }))}
                openMenu={openMenu}
                options={firstQueryOptions}
                value={String(form.firstRoundQueries)}
              />
              <ComposerParameterMenu
                icon={Repeat2}
                label={t.composer.maxRounds}
                menuKey="maxRounds"
                onOpenMenuChange={setOpenMenu}
                onValueChange={(value) => setForm((currentForm) => {
                  const maxRounds = Number(value) as ComposerFormState['maxRounds']
                  return {
                    ...currentForm,
                    maxRounds,
                    minRounds: Math.min(currentForm.minRounds, maxRounds) as ComposerFormState['minRounds'],
                  }
                })}
                openMenu={openMenu}
                options={maxRoundOptions}
                value={String(form.maxRounds)}
              />
              <DropdownMenu
                modal={false}
                onOpenChange={(isOpen) => setOpenMenu(isOpen ? 'more' : null)}
                open={openMenu === 'more'}
              >
                <Tooltip>
                  <DropdownMenuTrigger asChild>
                    <TooltipTrigger asChild>
                      <Button
                        aria-label={t.composer.moreSettings}
                        className={composerIconButtonClassName}
                        onPointerDown={(event) => {
                          if (openMenu === 'more') return

                          event.preventDefault()
                          event.currentTarget.focus()
                          switchComposerMenu(openMenu, 'more', setOpenMenu)
                        }}
                        type="button"
                        variant="ghost"
                      >
                        <SlidersHorizontal className="size-3.5" />
                      </Button>
                    </TooltipTrigger>
                  </DropdownMenuTrigger>
                  <TooltipContent>{t.composer.moreSettings}</TooltipContent>
                </Tooltip>
                <DropdownMenuContent align="start" className={optionMenuContentClassName} side="top" sideOffset={8}>
                  <OptionMenuHeader count={1} title={t.composer.moreSettings} />
                  <div className="py-1">
                    <div className="px-2.5 pb-1 pt-1.5">
                      <div className="flex items-center gap-1.5">
                        <Repeat2 className="icon-sm shrink-0 text-muted-foreground/70" />
                        <span className="t-list text-foreground">{t.composer.minRounds}</span>
                        <span className="ml-auto rounded-md bg-brand-subtle px-1.5 py-0.5 t-hint font-medium tabular-nums text-brand">
                          {form.minRounds}
                        </span>
                      </div>
                      <p className="mt-0.5 t-meta-sm text-muted-foreground">{t.composer.minRoundsDescription}</p>
                      <div className="mt-2 grid h-7 grid-cols-2 rounded-md bg-surface p-0.5">
                        {[1, 2].map((rounds) => {
                          const disabled = rounds > form.maxRounds
                          const active = form.minRounds === rounds
                          return (
                            <button
                              aria-pressed={active}
                              className={cn(
                                'inline-flex items-center justify-center gap-1 rounded px-2 t-meta-sm font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-40',
                                active && 'bg-brand-subtle text-brand shadow-[0_1px_2px_var(--shadow-hairline)] ring-1 ring-brand/20',
                                !active && !disabled && 'text-muted-foreground hover:bg-background hover:text-foreground',
                              )}
                              disabled={disabled}
                              key={rounds}
                              onClick={() => setForm((currentForm) => ({
                                ...currentForm,
                                minRounds: rounds as ComposerFormState['minRounds'],
                              }))}
                              type="button"
                            >
                              <span className="tabular-nums">{rounds}</span>
                              <span className="flex icon-xs items-center justify-center">
                                {active ? <Check className="icon-xs" /> : null}
                              </span>
                            </button>
                          )
                        })}
                      </div>
                    </div>
                  </div>
                </DropdownMenuContent>
              </DropdownMenu>
            </div>
            <div className="flex shrink-0 items-center gap-1">
              <QuotaMeter />
              <ComposerStatusMenu
                confidenceStop={form.confidenceStop}
                firstRoundQueries={form.firstRoundQueries}
                maxRounds={form.maxRounds}
                minRounds={form.minRounds}
                onOpenMenuChange={setOpenMenu}
                openMenu={openMenu}
                reportProfile={form.reportProfile}
                selectedStack={selectedStack}
              />
              <Button
                aria-label={isSubmitting ? t.composer.submitting : t.composer.send}
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
                {isSubmitting
                  ? <LoaderCircle className="size-4 animate-spin motion-reduce:animate-none" />
                  : <SendHorizontal className="size-4" />}
              </Button>
            </div>
          </div>
        </div>
        {submissionError ? <ResearchSubmissionAlert message={submissionError} /> : null}
        <ComposerDisclosureHint />
      </form>
    </motion.section>
  )
})

function ComposerStatusMenu({
  confidenceStop,
  firstRoundQueries,
  maxRounds,
  minRounds,
  onOpenMenuChange,
  openMenu,
  reportProfile,
  selectedStack,
}: {
  confidenceStop: ComposerFormState['confidenceStop']
  firstRoundQueries: ComposerFormState['firstRoundQueries']
  maxRounds: ComposerFormState['maxRounds']
  minRounds: ComposerFormState['minRounds']
  onOpenMenuChange: (menu: ComposerMenuKey | null) => void
  openMenu: ComposerMenuKey | null
  reportProfile: ComposerFormState['reportProfile']
  selectedStack: string
}) {
  const { t } = useLocale()
  const reportProfileLabel =
    reportProfile === 'schnell'
      ? t.composer.schnell
      : reportProfile === 'compact'
        ? t.composer.compact
        : t.composer.deep

  return (
    <DropdownMenu
      modal={false}
      onOpenChange={(isOpen) => onOpenMenuChange(isOpen ? 'summary' : null)}
      open={openMenu === 'summary'}
    >
      <Tooltip>
        <DropdownMenuTrigger asChild>
          <TooltipTrigger asChild>
            <Button
              aria-label={t.composer.settingsSummary}
              className={composerIconButtonClassName}
              onPointerDown={(event) => {
                if (openMenu === 'summary') return

                event.preventDefault()
                event.currentTarget.focus()
                switchComposerMenu(openMenu, 'summary', onOpenMenuChange)
              }}
              type="button"
              variant="ghost"
            >
              <ListChecks className="size-3.5" />
            </Button>
          </TooltipTrigger>
        </DropdownMenuTrigger>
        <TooltipContent>{t.composer.settingsSummary}</TooltipContent>
      </Tooltip>
      <DropdownMenuContent align="end" className={optionMenuContentClassName} side="top" sideOffset={8}>
        <OptionMenuHeader count={6} title={t.composer.settingsSummary} />
        <div className="py-1">
          <SummaryGroup label={t.composer.summaryStrategy}>
            <StatusRow label={t.common.stack} value={selectedStack} />
          </SummaryGroup>
          <DropdownMenuSeparator className="mx-0 my-1" />
          <SummaryGroup label={t.composer.summaryPlanning}>
            <StatusRow label={t.composer.firstQueries} value={String(firstRoundQueries)} />
            <StatusRow label={t.composer.minRounds} value={String(minRounds)} />
            <StatusRow label={t.composer.maxRounds} value={String(maxRounds)} />
          </SummaryGroup>
          <DropdownMenuSeparator className="mx-0 my-1" />
          <SummaryGroup label={t.composer.summaryStopAndOutput}>
            <StatusRow label={t.composer.confidenceTarget} value={`${confidenceStop} / 10`} />
            <StatusRow label={t.composer.reportProfile} value={reportProfileLabel} />
          </SummaryGroup>
        </div>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

function ComposerParameterMenu({
  icon: Icon,
  label,
  menuKey,
  onOpenMenuChange,
  onValueChange,
  openMenu,
  options,
  showValue = false,
  value,
}: {
  icon: LucideIcon
  label: string
  menuKey: ComposerMenuKey
  onOpenMenuChange: (menu: ComposerMenuKey | null) => void
  onValueChange: (value: string) => void
  openMenu: ComposerMenuKey | null
  options: ComposerOption[]
  showValue?: boolean
  value: string
}) {
  const selectedOption = options.find((option) => option.value === value)
  const valueLabel = selectedOption?.label ?? value
  const triggerLabel = `${label}: ${valueLabel}`

  return (
    <DropdownMenu
      modal={false}
      onOpenChange={(isOpen) => onOpenMenuChange(isOpen ? menuKey : null)}
      open={openMenu === menuKey}
    >
      <Tooltip>
        <TooltipTrigger asChild>
          <DropdownMenuTrigger asChild>
            <Button
              aria-label={triggerLabel}
              className={cn(
                composerIconButtonClassName,
                showValue ? 'w-auto gap-1 px-1.5' : 'w-10 gap-0.5 px-1',
              )}
              onPointerDown={(event) => {
                if (openMenu === menuKey) return

                event.preventDefault()
                event.currentTarget.focus()
                switchComposerMenu(openMenu, menuKey, onOpenMenuChange)
              }}
              type="button"
              variant="ghost"
            >
              <Icon className="size-3.5 shrink-0" />
              {showValue ? (
                <span className="max-w-20 truncate text-xs">{valueLabel}</span>
              ) : (
                <span className="sr-only">{valueLabel}</span>
              )}
            </Button>
          </DropdownMenuTrigger>
        </TooltipTrigger>
        <TooltipContent>{triggerLabel}</TooltipContent>
      </Tooltip>
      <DropdownMenuContent align="start" className={optionMenuContentClassName} side="top" sideOffset={8}>
        <OptionMenuHeader count={options.length} title={label} value={valueLabel} />
        <div className="py-1">
          {options.map((option) => (
            <OptionMenuItem
              active={option.value === value}
              description={option.description}
              icon={Icon}
              key={option.value}
              label={option.label}
              onSelect={() => onValueChange(option.value)}
            />
          ))}
        </div>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
