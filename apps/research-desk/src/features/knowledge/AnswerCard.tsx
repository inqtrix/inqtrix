import { useCallback, useEffect, useRef, useState, type MouseEvent, type SyntheticEvent } from 'react'
import { createPortal } from 'react-dom'
import { motion, useReducedMotion } from 'motion/react'
import { BadgeCheck, Check, ChevronRight, Copy, FileSearch, Info, ListChecks, MoreHorizontal, Quote, Type } from '@/components/icons'
import { Button } from '@/components/ui/button'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import {
  AssistantMenuHeader,
  AssistantMenuIcon,
  AssistantMenuLabel,
  assistantMenuContentClassName,
} from '@/components/ui/assistant-menu'
import { MarkdownRenderer } from '@/components/markdown/MarkdownRenderer'
import { MarkdownSelectionCopyMenu } from '@/components/markdown/MarkdownSelectionCopyMenu'
import { useLocale } from '@/i18n/LocaleProvider'
import { countLabel } from '@/i18n/plural'
import { cn } from '@/lib/utils'
import { appMotion } from '@/motion/transitions'
import type { KnowledgeAnswerRecord, KnowledgeReferenceRecord, KnowledgeRunStepRecord } from '@/features/project/types'
import { citationLabelFromHref, linkifyCitationLabels } from './answer'
import { formatAnswerForCopy, type AnswerCopyMode } from './copyAnswer'
import { citationViews, firstOpenableCitation, groupCitationsByDocument } from './citations'
import { CitationGroupList } from './CitationRow'
import { excerptHighlightRanges, HighlightedExcerpt, previewWindow } from './CitationExcerpt'
import { KnowledgeStepList } from './KnowledgeStepList'
import { profileDisplayName } from './stepLines'
import { RetrievalDegradationNotice } from './RetrievalDegradationNotice'
import {
  knowledgeRetrievalDegradationText,
  knowledgeSearchWarningNotice,
} from './retrievalDegradation'
import { copyTextToClipboard } from '@/lib/clipboard'
import { readerError } from '@/api/requestErrorText'

type CitationPreview = {
  title: string
  excerpt: string
  highlightTargets: string[]
  verified: boolean
  anchor: { top: number; bottom: number; left: number }
}

/**
 * Completed knowledge answer, rendered as an assistant bubble (avatar + body +
 * actions) so the Wissen view reads like the chat view. The markdown body has
 * clickable `[K#]`/bare-`K#` citations, the source list from the report
 * references, and a quiet meta line (profile, degraded stages, grounding).
 * Citation interception happens on a capture-phase click handler so the
 * off-limits Markdown renderer stays untouched.
 */
export function AnswerCard({
  answer,
  collectionCount,
  completedAtLabel,
  highlightEntry = false,
  onOpenReference,
  steps = [],
}: {
  answer: KnowledgeAnswerRecord
  collectionCount: number
  completedAtLabel?: string
  /** One-shot arrival choreography for the answer that just replaced a live run. */
  highlightEntry?: boolean
  onOpenReference: (reference: KnowledgeReferenceRecord) => void
  steps?: KnowledgeRunStepRecord[]
}) {
  const { t } = useLocale()
  const reduceMotion = Boolean(useReducedMotion())
  const [copied, setCopied] = useState(false)
  const [preview, setPreview] = useState<CitationPreview | null>(null)
  const [stepsOpen, setStepsOpen] = useState(false)
  const [sourcesOpen, setSourcesOpen] = useState(false)
  const hideTimerRef = useRef<number | null>(null)
  const hasSteps = steps.length > 0

  useEffect(() => () => {
    if (hideTimerRef.current) window.clearTimeout(hideTimerRef.current)
  }, [])

  const copyLabels = {
    aiDisclosure: t.aiTransparency.exportNotice,
    evidenceHeading: t.knowledge.copyEvidenceHeading,
    pageLabel: t.knowledge.citationPage,
    sectionLabel: t.knowledge.viewerSection,
    sourcesHeading: t.knowledge.sources,
    verifiedLabel: t.knowledge.viewerVerified,
  }
  const profileLabel = answer.profileId ? profileDisplayName(answer.profileId, t.knowledge) : null
  const searchWarningNotices = (answer.retrievalWarnings ?? []).map(
    (warning) => knowledgeSearchWarningNotice(warning, t.knowledge),
  )
  const retrievalWarningMessages = [
    ...(answer.retrievalDegradations ?? []).map(
      (degradation) => knowledgeRetrievalDegradationText(degradation, t.knowledge),
    ),
    ...searchWarningNotices
      .filter((notice) => notice.tone === 'warning')
      .map((notice) => notice.message),
  ]
  const retrievalInformationalMessages = searchWarningNotices
    .filter((notice) => notice.tone === 'informational')
    .map((notice) => notice.message)

  async function copy(mode: AnswerCopyMode) {
    try {
      if (!(await copyTextToClipboard(formatAnswerForCopy(answer, mode, copyLabels)))) {
        throw readerError('clipboardUnavailable')
      }
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1200)
    } catch (error) {
      console.warn('Inqtrix knowledge answer copy failed.', error)
    }
  }

  function handleCitationClick(event: MouseEvent<HTMLDivElement>) {
    const target = event.target as HTMLElement | null
    const anchor = target?.closest('a')
    const label = citationLabelFromHref(anchor?.getAttribute('href'))
    if (!label) return
    event.preventDefault()
    event.stopPropagation()
    const reference = answer.references.find((entry) => entry.label === label)
    if (reference) onOpenReference(reference)
  }

  function cancelHide() {
    if (hideTimerRef.current) {
      window.clearTimeout(hideTimerRef.current)
      hideTimerRef.current = null
    }
  }

  function scheduleHide() {
    cancelHide()
    hideTimerRef.current = window.setTimeout(() => setPreview(null), 140)
  }

  const dismissPreview = useCallback(() => setPreview(null), [])

  // Hovering or focusing a `[K#]` citation surfaces a quick-glance preview of
  // the cited passage (highlighted) — only for citations that carry an excerpt.
  // Bubbling mouseover/focusin retarget to the anchor; non-citation targets are
  // ignored without disturbing an open preview.
  function handleCitationHover(event: SyntheticEvent<HTMLDivElement>) {
    const anchor = (event.target as HTMLElement | null)?.closest('a')
    const label = citationLabelFromHref(anchor?.getAttribute('href'))
    if (!label || !anchor) return
    const reference = answer.references.find((entry) => entry.label === label)
    if (!reference?.excerpt) return
    cancelHide()
    const rect = anchor.getBoundingClientRect()
    const quote = answer.quotes.find((entry) => entry.label === label)
    setPreview({
      anchor: { bottom: rect.bottom, left: rect.left, top: rect.top },
      excerpt: reference.excerpt,
      highlightTargets: quote ? [quote.text] : reference.sourceText ? [reference.sourceText] : [],
      title: reference.title ?? reference.url,
      verified: quote?.verified ?? false,
    })
  }

  return (
    <motion.div
      animate={{ filter: 'blur(0px)', opacity: 1, y: 0 }}
      className={cn(
        'group/answer grid min-w-0 grid-cols-[minmax(0,1fr)]',
        highlightEntry && 'inqtrix-knowledge-answer-entry',
      )}
      data-knowledge-answer-entry={highlightEntry ? 'true' : undefined}
      initial={highlightEntry && !reduceMotion ? { filter: 'blur(3px)', opacity: 0, y: 14 } : false}
      transition={highlightEntry ? appMotion.panel : undefined}
    >
      <div className="min-w-0">
        <div className="mb-1 flex min-w-0 items-center gap-1.5">
          <div className="flex min-w-0 flex-wrap items-baseline gap-x-2 gap-y-0.5 t-meta-sm font-semibold text-muted-foreground">
            <span className="min-w-0">{answer.refusal ? t.knowledge.refusalTitle : t.knowledge.answerLabel}</span>
            {completedAtLabel && (
              <span className="tabular-nums">{completedAtLabel}</span>
            )}
          </div>
          {hasSteps && (
            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  aria-expanded={stepsOpen}
                  aria-label={t.knowledge.answerSteps}
                  className="h-6 w-10 shrink-0 gap-0.5 px-1 text-foreground/65 hover:text-foreground"
                  onClick={() => setStepsOpen((current) => !current)}
                  size="icon"
                  type="button"
                  variant="ghost"
                >
                  <ListChecks className="size-3.5" />
                  {/* App-wide disclosure convention: chevron points right
                      collapsed, down expanded (agent-desk parity). */}
                  <ChevronRight className={cn('size-3 transition-transform', stepsOpen && 'rotate-90')} />
                </Button>
              </TooltipTrigger>
              <TooltipContent>{t.knowledge.answerStepsHint}</TooltipContent>
            </Tooltip>
          )}
        </div>

        {hasSteps && stepsOpen && (
          <div className="mb-2 rounded-lg border border-border bg-surface/50 px-3 py-3">
            <KnowledgeStepList
              collectionCount={collectionCount}
              failed={false}
              steps={steps}
            />
          </div>
        )}

        {(retrievalInformationalMessages.length > 0 || retrievalWarningMessages.length > 0) && (
          <div className="mb-3 space-y-2">
            <RetrievalDegradationNotice
              announce={highlightEntry}
              messages={retrievalInformationalMessages}
              tone="informational"
            />
            <RetrievalDegradationNotice
              announce={highlightEntry}
              messages={retrievalWarningMessages}
            />
          </div>
        )}

        {answer.refusal ? (
          <div className="flex items-start gap-2.5 rounded-lg border border-border/70 bg-surface/60 p-3">
            <Info className="icon-sm mt-0.5 shrink-0 text-muted-foreground/70" />
            <p className="min-w-0 t-body text-muted-foreground">{answer.answerMarkdown}</p>
          </div>
        ) : (
          /* Capture-phase interception of the synthetic #kref-* citation links;
             mouseover/focus open the quick-glance preview, container exit
             (mouseleave/blur) schedules its dismissal. mouseleave fires ONCE on
             real exit (not per child), so hovering prose never flickers it. */
          <MarkdownSelectionCopyMenu
            aiGenerated
            className="text-sm leading-snug text-foreground"
            markdown={answer.answerMarkdown}
            onBlur={scheduleHide}
            onClickCapture={handleCitationClick}
            onFocus={handleCitationHover}
            onMouseLeave={scheduleHide}
            onMouseOver={handleCitationHover}
          >
            <MarkdownRenderer
              markdown={linkifyCitationLabels(
                answer.answerMarkdown,
                new Set(answer.references.map((reference) => reference.label)),
              )}
              variant="chat"
            />
          </MarkdownSelectionCopyMenu>
        )}

        {preview && (
          <CitationHoverPreview
            onDismiss={dismissPreview}
            onEnter={cancelHide}
            onLeave={scheduleHide}
            preview={preview}
          />
        )}

        {answer.references.length > 0 && (
          <div className="mt-4 border-t border-border/70 pt-3">
            {/* Default-collapsed disclosure (agent-desk parity): a long
                citation list must not dominate the conversation. */}
            <button
              aria-expanded={sourcesOpen}
              className="flex items-center gap-1 t-meta-sm font-semibold text-muted-foreground transition-colors hover:text-foreground"
              onClick={() => setSourcesOpen((current) => !current)}
              type="button"
            >
              {countLabel(
                answer.references.length,
                t.knowledge.sourcesSummaryOne,
                t.knowledge.sourcesSummary,
              )}
              <ChevronRight
                className={cn(
                  'size-3 shrink-0 transition-transform',
                  sourcesOpen && 'rotate-90',
                )}
              />
            </button>
            {sourcesOpen && <div className="mt-1.5">
              <CitationGroupList
                activeKey={null}
                groups={groupCitationsByDocument(
                  citationViews(answer.references, answer.quotes, t.knowledge.viewerSection),
                )}
                onOpen={(picked) => onOpenReference(picked.reference)}
                onOpenDocument={(group) => {
                  const view = firstOpenableCitation(group)
                  if (view) onOpenReference(view.reference)
                }}
              />
            </div>}
          </div>
        )}

        <div className="mt-3 flex min-w-0 items-center gap-1 text-muted-foreground">
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                aria-label={copied ? t.knowledge.answerCopied : t.knowledge.answerCopy}
                className={cn(
                  'size-6 shrink-0 text-foreground/65 hover:text-foreground',
                  copied && 'text-success hover:text-success',
                )}
                onClick={() => void copy('sources')}
                size="icon"
                type="button"
                variant="ghost"
              >
                {copied ? <Check className="size-3" /> : <Copy className="size-3" />}
              </Button>
            </TooltipTrigger>
            <TooltipContent>{copied ? t.knowledge.answerCopied : t.knowledge.answerCopy}</TooltipContent>
          </Tooltip>
          <AnswerCopyMenu
            completedAtLabel={completedAtLabel}
            onCopy={(mode) => void copy(mode)}
            profileLabel={profileLabel}
          />
          <MetaLine answer={answer} />
        </div>

      </div>
    </motion.div>
  )
}

/**
 * The "more copy options" dropdown beside the copy button, sharing the chat
 * assistant menu's primitives so both views read as one design language. The
 * copy icon itself handles the common case (answer + source list); this menu
 * offers the two variants: copy with the full evidence excerpts, or the bare
 * answer text.
 */
function AnswerCopyMenu({
  completedAtLabel,
  onCopy,
  profileLabel,
}: {
  completedAtLabel?: string
  onCopy: (mode: AnswerCopyMode) => void
  profileLabel?: string | null
}) {
  const { t } = useLocale()
  return (
    <DropdownMenu modal={false}>
      <Tooltip>
        <TooltipTrigger asChild>
          <DropdownMenuTrigger asChild>
            <Button
              aria-label={t.knowledge.copyOptions}
              className="size-6 shrink-0 text-foreground/65 transition-colors hover:text-foreground data-[state=open]:bg-accent data-[state=open]:text-foreground"
              onClick={(event) => event.stopPropagation()}
              size="icon"
              type="button"
              variant="ghost"
            >
              <MoreHorizontal className="size-3" />
            </Button>
          </DropdownMenuTrigger>
        </TooltipTrigger>
        <TooltipContent>{t.knowledge.copyOptions}</TooltipContent>
      </Tooltip>
      <DropdownMenuContent align="start" className={assistantMenuContentClassName} side="top" sideOffset={6}>
        <AssistantMenuHeader primary={completedAtLabel ?? t.knowledge.answerLabel} secondary={profileLabel} />
        <div className="p-1">
          <DropdownMenuItem className="group gap-2 rounded-md px-2 py-1.5" onSelect={() => onCopy('evidence')}>
            <AssistantMenuIcon icon={Quote} />
            <AssistantMenuLabel>{t.knowledge.copyWithEvidence}</AssistantMenuLabel>
          </DropdownMenuItem>
          <DropdownMenuItem className="group gap-2 rounded-md px-2 py-1.5" onSelect={() => onCopy('answer')}>
            <AssistantMenuIcon icon={Type} />
            <AssistantMenuLabel>{t.knowledge.copyAnswerOnly}</AssistantMenuLabel>
          </DropdownMenuItem>
        </div>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

function MetaLine({ answer }: { answer: KnowledgeAnswerRecord }) {
  const { t } = useLocale()
  const parts: string[] = []
  if (answer.profileId) {
    const profile = profileDisplayName(answer.profileId, t.knowledge)
    parts.push(
      answer.autoSelected
        ? `${t.knowledge.profileRanLabel.replace('{profile}', profile)} (${t.knowledge.autoSelectedSuffix})`
        : t.knowledge.profileRanLabel.replace('{profile}', profile),
    )
  }
  if (typeof answer.evidenceUsed === 'number' && answer.evidenceUsed > 0) {
    // Makes the deep profile's wider evidence breadth perceivable: how many
    // passages were grounded in, and across how many distinct documents (so a
    // multi-document answer is visibly multi-document).
    const distinctDocs = new Set(
      answer.references.map((reference) => reference.documentId).filter(Boolean),
    ).size
    parts.push(
      distinctDocs > 1
        ? t.knowledge.evidenceUsedDocsMeta
            .replace('{chunks}', String(answer.evidenceUsed))
            .replace('{docs}', String(distinctDocs))
        : countLabel(answer.evidenceUsed, t.knowledge.evidenceUsedMetaOne, t.knowledge.evidenceUsedMeta),
    )
  }
  if (answer.degradedStages.length > 0) {
    parts.push(t.knowledge.profileDegradedHint.replace('{stages}', answer.degradedStages.join(', ')))
  }
  if (answer.grounding?.degraded) {
    parts.push(t.knowledge.groundingUnavailableMeta)
  } else if (answer.grounding && answer.grounding.total > 0) {
      parts.push(
        countLabel(answer.grounding.total, t.knowledge.groundingMetaOne, t.knowledge.groundingMeta)
          .replace('{verified}', String(answer.grounding.verified)),
      )
  }
  // Surface the evidence gate on the completed answer (the live step ledger
  // shows it during the run) — but only when it is informative: more than one
  // judgement round, or it answered without full sufficiency (early-stop /
  // partial coverage).
  if (answer.gate && answer.gate.maxRounds > 0 && (answer.gate.roundsUsed > 1 || !answer.gate.sufficient)) {
    const stoppedEarly = !answer.gate.sufficient && answer.gate.roundsUsed < answer.gate.maxRounds
    const rounds = countLabel(answer.gate.maxRounds, t.knowledge.gateRoundsMetaOne, t.knowledge.gateRoundsMeta)
      .replace('{used}', String(answer.gate.roundsUsed))
    parts.push(stoppedEarly ? `${rounds} · ${t.knowledge.gateStoppedEarly}` : rounds)
  }
  if (parts.length === 0) return null
  return (
    <p className="min-w-0 t-meta text-muted-foreground">{parts.join(' · ')}</p>
  )
}

const PREVIEW_WIDTH = 340
const PREVIEW_MAX_HEIGHT = 280

/**
 * Quick-glance preview of a cited passage, portaled to the body so it escapes
 * the answer bubble's transform/overflow context, and pinned beside the hovered
 * `[K#]` (above the anchor when there's no room below). Shows the cited span
 * highlighted in its immediate context — the same two-level highlight as the
 * "Beleg" panel, for visual continuity. Clicking the marker opens the full panel.
 */
function CitationHoverPreview({
  onDismiss,
  onEnter,
  onLeave,
  preview,
}: {
  onDismiss: () => void
  onEnter: () => void
  onLeave: () => void
  preview: CitationPreview
}) {
  const { t } = useLocale()
  const { anchor, excerpt, highlightTargets, title, verified } = preview
  const windowed = previewWindow(excerpt, excerptHighlightRanges(excerpt, highlightTargets))
  const popoverRef = useRef<HTMLDivElement | null>(null)

  // The popover is position:fixed against the captured anchor rect, so a page
  // scroll moves the `[K#]` out from under it — dismiss then. But scrolling the
  // popover's OWN content (reading a long excerpt) also fires a capture-phase
  // scroll event; ignore those, or the popover vanishes the moment the user
  // scrolls it.
  useEffect(() => {
    const onScroll = (event: Event) => {
      const target = event.target
      if (popoverRef.current && target instanceof Node && popoverRef.current.contains(target)) {
        return
      }
      onDismiss()
    }
    window.addEventListener('scroll', onScroll, true)
    window.addEventListener('resize', onDismiss)
    return () => {
      window.removeEventListener('scroll', onScroll, true)
      window.removeEventListener('resize', onDismiss)
    }
  }, [onDismiss])

  const left = Math.max(8, Math.min(anchor.left, window.innerWidth - PREVIEW_WIDTH - 8))
  const spaceBelow = window.innerHeight - anchor.bottom
  const placeBelow = spaceBelow > 220
  // Bound the box to a fixed ceiling (scroll within) instead of letting it grow
  // to the full available viewport height for a short excerpt near the top.
  const position: { left: number; maxHeight: number; top?: number; bottom?: number } = placeBelow
    ? { left, maxHeight: Math.min(PREVIEW_MAX_HEIGHT, Math.max(120, spaceBelow - 16)), top: anchor.bottom + 6 }
    : { bottom: window.innerHeight - anchor.top + 6, left, maxHeight: Math.min(PREVIEW_MAX_HEIGHT, Math.max(120, anchor.top - 16)) }

  return createPortal(
    <div
      className="fixed z-50 w-[340px] overflow-y-auto rounded-lg border border-border bg-popover p-3 text-popover-foreground shadow-md"
      onMouseEnter={onEnter}
      onMouseLeave={onLeave}
      ref={popoverRef}
      style={position}
    >
      <div className="mb-1.5 flex items-center gap-1.5">
        <FileSearch className="icon-xs shrink-0 text-muted-foreground/70" />
        <span className="min-w-0 flex-1 truncate t-meta-sm font-medium text-foreground">{title}</span>
        {verified && (
          <span className="inline-flex shrink-0 items-center gap-0.5 rounded bg-success-subtle px-1 py-0.5 t-hint font-medium text-success">
            <BadgeCheck className="icon-xs" />
            {t.knowledge.viewerVerified}
          </span>
        )}
      </div>
      <blockquote className="border-l-2 border-brand/40 pl-2.5 t-meta leading-6 text-foreground/90">
        <HighlightedExcerpt ranges={windowed.ranges} text={windowed.text} />
      </blockquote>
    </div>,
    document.body,
  )
}
