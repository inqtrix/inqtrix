import { Check, Sparkles, X } from '@/components/icons'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'
import { appMotion } from '@/motion/transitions'
import { motion } from 'motion/react'
import { useEffect, useMemo, useRef, useState } from 'react'
import { diffTextForDisplay } from './diffTextForDisplay'
import type { TextImproveReviewLabels, TextImprovementProposal } from './types'

const COMMIT_DELAY_MS = 220

export function TextImproveReviewPanel({
  className,
  contentClassName,
  density = 'comfortable',
  fill = false,
  labels,
  onAccept,
  onReject,
  proposal,
  reduceMotion,
}: {
  className?: string
  contentClassName?: string
  density?: 'comfortable' | 'compact'
  fill?: boolean
  labels: TextImproveReviewLabels
  onAccept: (text: string) => void
  onReject: () => void
  proposal: TextImprovementProposal
  reduceMotion: boolean | null
}) {
  const [isAccepting, setIsAccepting] = useState(false)
  const acceptTimeoutRef = useRef<number | null>(null)
  const diffTokens = useMemo(
    () => diffTextForDisplay(proposal.originalText, proposal.improvedText),
    [proposal.improvedText, proposal.originalText],
  )

  useEffect(() => {
    if (acceptTimeoutRef.current !== null) {
      window.clearTimeout(acceptTimeoutRef.current)
      acceptTimeoutRef.current = null
    }
    setIsAccepting(false)
  }, [proposal])

  useEffect(() => () => {
    if (acceptTimeoutRef.current !== null) {
      window.clearTimeout(acceptTimeoutRef.current)
    }
  }, [])

  function acceptProposal() {
    if (isAccepting) return
    if (reduceMotion) {
      onAccept(proposal.improvedText)
      return
    }

    setIsAccepting(true)
    acceptTimeoutRef.current = window.setTimeout(() => {
      acceptTimeoutRef.current = null
      onAccept(proposal.improvedText)
    }, COMMIT_DELAY_MS)
  }

  return (
    <motion.div
      animate={
        isAccepting && !reduceMotion
          ? {
            boxShadow: [
              '0 18px 48px var(--shadow-soft)',
              '0 0 0 3px color-mix(in oklch, var(--brand) 18%, transparent), 0 22px 58px var(--shadow-soft)',
              '0 18px 48px var(--shadow-soft)',
            ],
            scale: [1, 0.99, 1.012, 1],
          }
          : { boxShadow: '0 18px 48px var(--shadow-soft)', scale: 1 }
      }
      className={cn(
        'overflow-hidden rounded-md border border-brand/25 bg-card/96 text-foreground shadow-[0_18px_48px_var(--shadow-soft)] backdrop-blur-xl',
        fill && 'flex h-full min-h-0 flex-col',
        className,
      )}
      transition={{ duration: 0.24, ease: appMotion.panel.ease }}
    >
      <div className="flex min-h-11 items-center justify-between gap-3 border-b border-border/70 bg-background/88 px-3 py-2">
        <div className="flex min-w-0 items-center gap-2">
          <span className="grid size-6 shrink-0 place-items-center rounded-md bg-brand-subtle text-brand">
            <Sparkles className="size-3.5" />
          </span>
          <span className="truncate text-xs font-semibold tracking-normal text-foreground">
            {labels.title}
          </span>
        </div>
        <div className="flex shrink-0 items-center gap-1">
          <Button
            aria-label={labels.reject}
            className="size-7 text-muted-foreground hover:text-destructive"
            disabled={isAccepting}
            onClick={onReject}
            size="icon"
            type="button"
            variant="ghost"
          >
            <X className="size-3.5" />
          </Button>
          <motion.span
            animate={isAccepting && !reduceMotion ? { scale: [1, 0.82, 1.18, 1] } : { scale: 1 }}
            className="inline-flex"
            transition={{ duration: 0.2, ease: appMotion.composer.ease }}
          >
            <Button
              aria-label={labels.accept}
              className="size-7 bg-brand text-brand-foreground shadow-sm hover:bg-brand/90 hover:text-brand-foreground"
              disabled={isAccepting}
              onClick={acceptProposal}
              size="icon"
              type="button"
            >
              <Check className="size-3.5" />
            </Button>
          </motion.span>
        </div>
      </div>
      <div
        className={cn(
          'space-y-2.5 px-3 py-3',
          density === 'compact' && 'space-y-2 px-3 py-2.5',
          fill && 'min-h-0 flex-1 overflow-y-auto',
          contentClassName,
        )}
      >
        <p
          className={cn(
            'whitespace-pre-wrap text-sm leading-6 text-foreground',
            density === 'comfortable' && 't-body',
          )}
        >
          {diffTokens.map((token, index) => (
            <span
              className={cn(
                token.status === 'changed'
                && 'rounded-[4px] bg-brand/10 px-0.5 text-brand shadow-[0_0_0_1px_color-mix(in_oklch,var(--brand)_24%,transparent)]',
              )}
              key={`${token.text}-${index}`}
            >
              {token.text}
            </span>
          ))}
        </p>
        {proposal.changeSummary.length > 0 ? (
          <div className="flex flex-wrap gap-1.5">
            {proposal.changeSummary.map((item) => (
              <span
                className="inline-flex max-w-full items-center rounded-md border border-border/70 bg-background/80 px-2 py-1 t-meta-sm font-medium leading-4 text-muted-foreground"
                key={item}
              >
                <span className="truncate">{item}</span>
              </span>
            ))}
          </div>
        ) : (
          <p className="t-meta-sm font-medium text-muted-foreground">
            {proposal.originalText.trim() === proposal.improvedText.trim()
              ? labels.noChanges
              : labels.changes}
          </p>
        )}
        {(proposal.warnings.length > 0 || proposal.clarificationQuestions.length > 0) && (
          <div className="rounded-md border border-warning/20 bg-background/80 px-2 py-1.5 t-meta-sm leading-5 text-warning">
            <span className="font-semibold">{labels.warnings}</span>
            {' '}
            {[...proposal.warnings, ...proposal.clarificationQuestions].join(' ')}
          </div>
        )}
      </div>
    </motion.div>
  )
}
