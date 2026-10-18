import { PanelBottomOpen, Users } from '@/components/icons'
import { AnimatePresence, motion } from 'motion/react'
import { Skeleton } from '@/components/ui/skeleton'
import { useCallback, useEffect, useRef, useState } from 'react'
import { Button } from '@/components/ui/button'
import { PanelToggle } from '@/components/ui/panel-toggle'
import { ScrollArea } from '@/components/ui/scroll-area'
import { WelcomeState } from '@/components/ui/welcome-state'
import type { CreateResearchRunRequest } from '@/features/researchRuns/types'
import { partitionJobsByAccess } from '@/features/sharing/shareModel'
import { useLocale } from '@/i18n/LocaleProvider'
import { StructuralLoadBoundary } from '@/motion/StructuralLoadBoundary'
import { appMotion } from '@/motion/transitions'
import type { JobFilter, ResearchJob } from '../types'
import {
  browserResearchDraftStorage,
  saveResearchDraftRecovery,
  takeResearchDraftRecovery,
} from '../researchDraftRecovery'
import type { ResearchSubmissionOutcome } from '../researchSubmission'
import {
  Composer,
  buildComposerRequest,
  defaultComposerFormState,
} from './Composer'
import { JobFilterMenu } from './JobFilterMenu'
import { ResearchJobCard } from './ResearchJobCard'

type ResearchRunColumnProps = {
  /** True while the server run listing is still in flight (never in demo /
   * local-first). The column then shows the card silhouette instead of the
   * empty-state hero — the same waiting language every other region speaks. */
  runsLoading?: boolean
  activeFilter: JobFilter
  allJobs: ResearchJob[]
  authenticatedUserId: string | null
  cancelErrorByRunId: Record<string, string>
  cancelSubmittingRunIds: ReadonlySet<string>
  expandedJobId: string | null
  isComposerVisible: boolean
  isReportVisible: boolean
  /** Disable run submission (composer send + suggestion buttons) while the
   * auth session is still resolving, instead of a silent no-op. */
  isSubmitDisabled: boolean
  jobs: ResearchJob[]
  onActiveFilterChange: (filter: JobFilter) => void
  onCancelJob: (jobId: string) => void
  onAuthenticationRequired: () => void
  onComposerSubmit: (request: CreateResearchRunRequest) => Promise<ResearchSubmissionOutcome>
  onComposerVisibleChange: (isComposerVisible: boolean) => void
  onReportVisibleChange: (isVisible: boolean) => void
  onResearchQuestionChange: (question: string) => void
  researchQuestion: string
  onDeleteJob: (jobId: string) => void
  onSelectJob: (jobId: string) => void
  onShareJob?: (jobId: string) => void
  onToggleJob: (jobId: string) => void
  reduceMotion: boolean | null
  reportPanelId?: string
  selectedJobId: string | null
  selectedStack: string
  shareCountByRunId?: Record<string, number>
}

export function ResearchRunColumn({
  activeFilter,
  allJobs,
  authenticatedUserId,
  cancelErrorByRunId,
  cancelSubmittingRunIds,
  expandedJobId,
  isComposerVisible,
  isReportVisible,
  isSubmitDisabled,
  jobs,
  runsLoading = false,
  onActiveFilterChange,
  onAuthenticationRequired,
  onCancelJob,
  onComposerSubmit,
  onComposerVisibleChange,
  onReportVisibleChange,
  onResearchQuestionChange,
  researchQuestion,
  onDeleteJob,
  onSelectJob,
  onShareJob,
  onToggleJob,
  reduceMotion,
  reportPanelId,
  selectedJobId,
  selectedStack,
  shareCountByRunId,
}: ResearchRunColumnProps) {
  const { t } = useLocale()
  // The shell keeps ordinary navigation drafts in memory. Auth recovery is the
  // narrower exception: after a rejected write, the complete form is restored
  // once for the same user across the mandatory account-boundary reload.
  const [composerForm, setComposerForm] = useState(() => ({
    ...defaultComposerFormState,
    question: researchQuestion,
  }))
  const composerFormRef = useRef(composerForm)
  composerFormRef.current = composerForm
  const submissionPendingRef = useRef(false)
  const [isSubmitting, setIsSubmitting] = useState(false)
  const [submissionError, setSubmissionError] = useState<string | null>(null)
  // Mirror question edits (and the submit-time clear) back up to the shell.
  useEffect(() => {
    onResearchQuestionChange(composerForm.question)
  }, [composerForm.question, onResearchQuestionChange])
  useEffect(() => {
    if (!authenticatedUserId) return
    const storage = browserResearchDraftStorage()
    if (!storage) return
    const recovered = takeResearchDraftRecovery(storage, authenticatedUserId)
    if (recovered) setComposerForm(recovered)
  }, [authenticatedUserId])
  const submitComposerRequest = useCallback(async (
    request: CreateResearchRunRequest,
  ): Promise<boolean> => {
    if (isSubmitDisabled || submissionPendingRef.current) return false
    submissionPendingRef.current = true
    setIsSubmitting(true)
    setSubmissionError(null)
    try {
      const outcome = await onComposerSubmit(request)
      if (outcome.status === 'accepted') return true

      setSubmissionError(outcome.message)
      if (outcome.recoverability === 'login') {
        if (authenticatedUserId) {
          const storage = browserResearchDraftStorage()
          const currentForm = composerFormRef.current
          const recoveryForm = currentForm.question.trim()
            ? currentForm
            : { ...currentForm, question: request.question }
          const stored = storage
            ? saveResearchDraftRecovery(storage, authenticatedUserId, recoveryForm)
            : false
          if (!stored) {
            console.warn('Research auth-recovery draft could not be stored in this browser tab.')
          }
        }
        onAuthenticationRequired()
      }
      return false
    } catch (error) {
      console.warn('Research submission recovery failed.', error)
      setSubmissionError(t.composer.submitFailed)
      return false
    } finally {
      submissionPendingRef.current = false
      setIsSubmitting(false)
    }
  }, [
    authenticatedUserId,
    isSubmitDisabled,
    onAuthenticationRequired,
    onComposerSubmit,
    t.composer.submitFailed,
  ])
  const { own: ownJobs, shared: sharedJobs } = partitionJobsByAccess(jobs)

  return (
    <section className="inqtrix-contained-panel relative flex h-full min-h-0 w-full min-w-0 flex-1 flex-col overflow-hidden bg-background">
      <JobFilterMenu
        activeFilter={activeFilter}
        jobs={allJobs}
        onActiveFilterChange={onActiveFilterChange}
        trailing={
          <PanelToggle
            collapseLabel={t.report.hide}
            controlsId={reportPanelId}
            expandLabel={t.report.show}
            expanded={isReportVisible}
            onToggle={onReportVisibleChange}
            side="right"
          />
        }
      />
      {allJobs.length === 0 && !runsLoading ? (
        <ResearchEmptyState
          disabled={isSubmitDisabled || isSubmitting}
          onSuggestionSelect={(question) => void submitComposerRequest(
            buildComposerRequest(composerFormRef.current, question, selectedStack),
          )}
        />
      ) : null}
      <div className="relative flex min-h-0 flex-1 flex-col gap-3 px-4 pt-3">
        {/* The first authoritative listing mounts behind the card silhouette.
            AnimatePresence remains absent until jobs exist, so hydrated
            history is not misclassified as newly arriving work. */}
        <StructuralLoadBoundary
          className="min-h-0 flex-1"
          fallback={<RunListSkeleton />}
          identity="research:runs"
          phase={runsLoading && allJobs.length === 0
            ? 'pending'
            : allJobs.length === 0 ? 'empty' : 'ready'}
        >
        {allJobs.length === 0 ? (
          <div className="min-h-0 flex-1" />
        ) : (
          <ScrollArea className="min-h-0 flex-1 pr-2">
            <motion.div
              layout
              className="flex flex-col gap-2.5 pb-3"
              transition={appMotion.list}
            >
              <AnimatePresence initial={false} mode={reduceMotion ? 'sync' : 'popLayout'}>
                {ownJobs.map((job) => (
                  <ResearchJobCard
                    isExpanded={expandedJobId === job.id}
                    isSelected={selectedJobId === job.id}
                    job={job}
                    key={job.id}
                    cancelError={cancelErrorByRunId[job.id]}
                    isCancelSubmitting={cancelSubmittingRunIds.has(job.id)}
                    onCancel={() => onCancelJob(job.id)}
                    onDelete={() => onDeleteJob(job.id)}
                    onSelect={() => onSelectJob(job.id)}
                    onShare={onShareJob ? () => onShareJob(job.id) : undefined}
                    onToggleExpanded={() => onToggleJob(job.id)}
                    shareCount={shareCountByRunId?.[job.id]}
                  />
                ))}
                {sharedJobs.length > 0 && (
                  <motion.div
                    animate={{ opacity: 1 }}
                    className="flex items-center gap-1.5 pt-1.5 text-muted-foreground"
                    exit={{ opacity: 0 }}
                    initial={reduceMotion ? false : { opacity: 0 }}
                    key="shared-with-me-divider"
                    transition={appMotion.list}
                  >
                    <Users className="size-3.5" />
                    <span className="t-caption">{t.sharing.sharedWithMe}</span>
                  </motion.div>
                )}
                {sharedJobs.map((job) => (
                  <ResearchJobCard
                    isExpanded={expandedJobId === job.id}
                    isSelected={selectedJobId === job.id}
                    job={job}
                    key={job.id}
                    cancelError={cancelErrorByRunId[job.id]}
                    isCancelSubmitting={cancelSubmittingRunIds.has(job.id)}
                    onCancel={() => onCancelJob(job.id)}
                    onDelete={() => onDeleteJob(job.id)}
                    onSelect={() => onSelectJob(job.id)}
                    onToggleExpanded={() => onToggleJob(job.id)}
                  />
                ))}
              </AnimatePresence>
              {jobs.length === 0 && (
                <div className="flex min-h-40 items-center justify-center rounded-lg border border-dashed border-border px-4 text-sm text-muted-foreground">
                  {t.home.emptyFilter}
                </div>
              )}
            </motion.div>
          </ScrollArea>
        )}
        </StructuralLoadBoundary>
      </div>

      <AnimatePresence initial={false} mode={reduceMotion ? 'sync' : 'popLayout'}>
        {isComposerVisible ? (
          <Composer
            form={composerForm}
            isSubmitting={isSubmitting}
            key="composer"
            onHide={() => onComposerVisibleChange(false)}
            onSubmit={submitComposerRequest}
            reduceMotion={reduceMotion}
            selectedStack={selectedStack}
            setForm={setComposerForm}
            submitDisabled={isSubmitDisabled || isSubmitting}
            submissionError={submissionError}
          />
        ) : (
          <motion.div
            key="composer-collapsed"
            initial={reduceMotion ? false : { opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={appMotion.composer}
            className="shrink-0 px-4 pb-4 pt-2"
          >
            <Button
              aria-label={t.composer.show}
              className="h-8 gap-1.5 rounded-md"
              onClick={() => onComposerVisibleChange(true)}
              size="sm"
              type="button"
              variant="outline"
            >
              <PanelBottomOpen className="size-4" />
              {t.composer.show}
            </Button>
          </motion.div>
        )}
      </AnimatePresence>
    </section>
  )
}

function ResearchEmptyState({
  disabled,
  onSuggestionSelect,
}: {
  disabled?: boolean
  onSuggestionSelect: (question: string) => void
}) {
  const { t } = useLocale()
  const suggestions = [
    {
      label: t.home.suggestionAiNews,
      question: t.home.suggestionAiNewsPrompt,
    },
    {
      label: t.home.suggestionExplainRag,
      question: t.home.suggestionExplainRagPrompt,
    },
    {
      label: t.home.suggestionCompareLlms,
      question: t.home.suggestionCompareLlmsPrompt,
    },
  ]

  return (
    <div className="pointer-events-none absolute inset-x-4 bottom-40 top-12 z-10 flex items-center justify-center px-4 py-8">
      <WelcomeState
        actions={(
          <div className="flex flex-wrap justify-center gap-2">
            {suggestions.map((suggestion) => (
              <Button
                className="h-8 rounded-md px-2.5 text-xs"
                disabled={disabled}
                key={suggestion.label}
                onClick={() => onSuggestionSelect(suggestion.question)}
                type="button"
                variant="outline"
              >
                <span>{suggestion.label}</span>
              </Button>
            ))}
          </div>
        )}
        body={(
          <>
            <p>{t.home.emptyBody}</p>
            <p>{t.home.emptyGuidance}</p>
          </>
        )}
        className="pointer-events-auto"
        example={t.home.emptyExample}
        kicker={t.home.emptyKicker}
        subtitle={t.home.emptyDescription}
        title={t.home.emptyTitle}
      />
    </div>
  )
}

/** Card silhouettes for the run listing while the server hydration is in
 * flight: same rounded card footprint the real run cards occupy, filling
 * the column so arriving cards land inside the silhouette instead of
 * popping into an empty-state hero. */
function RunListSkeleton() {
  return (
    <div aria-hidden className="flex min-h-0 flex-1 flex-col gap-2.5 overflow-hidden pr-2">
      {Array.from({ length: 5 }, (_, index) => (
        <div className="rounded-lg border border-border bg-card p-4" key={index}>
          <div className="flex flex-col gap-2.5">
            <div className="flex items-center justify-between gap-3">
              <Skeleton className="h-4 w-[62%]" />
              <Skeleton className="h-5 w-24 rounded-full" />
            </div>
            <Skeleton className="h-3.5 w-[48%]" />
          </div>
        </div>
      ))}
    </div>
  )
}
