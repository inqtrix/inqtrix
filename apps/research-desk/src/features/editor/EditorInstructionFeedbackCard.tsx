import { useEffect, useState } from 'react'
import { AnimatePresence, motion } from 'motion/react'
import { AlertTriangle, ChevronDown, LoaderCircle, Sparkles, X } from '@/components/icons'
import { cn } from '@/lib/utils'
import type { EditorInstructionFeedback } from './useEditorSuggestions'

type EditorInstructionFeedbackCardLabels = {
  assistantDone: string
  assistantThinking: string
  dismiss: string
  hide: string
  show: string
}

export function EditorInstructionFeedbackCard({
  feedback,
  labels,
  onDismiss,
}: {
  feedback: EditorInstructionFeedback | null
  labels: EditorInstructionFeedbackCardLabels
  onDismiss: () => void
}) {
  const [isExpanded, setIsExpanded] = useState(true)

  useEffect(() => {
    if (feedback) setIsExpanded(true)
  }, [feedback])

  return (
    <AnimatePresence initial={false}>
      {feedback ? (
        <motion.div
          animate={{ height: 'auto', opacity: 1, y: 0 }}
          className={cn(
            'mb-2 overflow-hidden rounded-md border shadow-sm',
            feedback.state === 'error'
              ? 'border-destructive/30 bg-destructive-subtle/35'
              : 'border-brand/25 bg-brand-subtle/25',
          )}
          exit={{ height: 0, opacity: 0, y: 4 }}
          initial={{ height: 0, opacity: 0, y: 4 }}
          transition={{ duration: 0.18 }}
        >
          <div className="flex items-center gap-2 px-3 py-2">
            {feedback.state === 'thinking' ? (
              <LoaderCircle className="size-3.5 animate-spin text-brand" />
            ) : feedback.state === 'error' ? (
              <AlertTriangle className="size-3.5 text-destructive" />
            ) : (
              <Sparkles className="size-3.5 text-brand" />
            )}
            <span className="t-label min-w-0 flex-1 truncate text-foreground">
              {feedback.state === 'thinking' ? labels.assistantThinking : labels.assistantDone}
              {typeof feedback.editCount === 'number' ? ` · ${feedback.editCount}` : ''}
            </span>
            <button
              aria-label={isExpanded ? labels.hide : labels.show}
              className="rounded-sm p-0.5 text-muted-foreground transition hover:bg-background/70 hover:text-foreground"
              onClick={() => setIsExpanded((value) => !value)}
              type="button"
            >
              <ChevronDown className={cn('size-3.5 transition-transform', !isExpanded && '-rotate-90')} />
            </button>
            <button
              aria-label={labels.dismiss}
              className="rounded-sm p-0.5 text-muted-foreground transition hover:bg-background/70 hover:text-foreground"
              onClick={onDismiss}
              type="button"
            >
              <X className="size-3.5" />
            </button>
          </div>
          <AnimatePresence initial={false}>
            {isExpanded ? (
              <motion.div
                animate={{ height: 'auto', opacity: 1 }}
                className="border-t border-border/60 px-3 py-2"
                exit={{ height: 0, opacity: 0 }}
                initial={{ height: 0, opacity: 0 }}
                transition={{ duration: 0.16 }}
              >
                <p className="t-meta text-muted-foreground">{feedback.message}</p>
                {feedback.warnings?.length ? (
                  <ul className="mt-1.5 space-y-0.5">
                    {feedback.warnings.map((warning, index) => (
                      <li className="t-meta-sm text-warning" key={`${index}-${warning}`}>
                        {warning}
                      </li>
                    ))}
                  </ul>
                ) : null}
              </motion.div>
            ) : null}
          </AnimatePresence>
        </motion.div>
      ) : null}
    </AnimatePresence>
  )
}
