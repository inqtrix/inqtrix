import { useState } from 'react'
import { motion, useReducedMotion } from 'motion/react'
import {
  AlertTriangle,
  ChevronDown,
  Sparkles,
  Square,
} from '@/components/icons'
import { Button } from '@/components/ui/button'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { useLocale } from '@/i18n/LocaleProvider'
import { cn } from '@/lib/utils'
import { appMotion } from '@/motion/transitions'
import type { KnowledgeThreadItemRecord } from '@/features/project/types'
import { KnowledgeStepList } from './KnowledgeStepList'
import { knowledgeRunFacts, knowledgeRunHeaderStatus } from './knowledgeRunHeader'

/**
 * Live surface for one running or terminal knowledge ask. In the composer dock
 * it is intentionally text-first: the step ledger is the trust surface, while
 * facts stay as compact inline metadata.
 */

export function KnowledgeRunCard({
  collectionCount,
  item,
  presentation = 'card',
}: {
  collectionCount: number
  item: KnowledgeThreadItemRecord
  presentation?: 'card' | 'dock'
}) {
  const { t } = useLocale()
  const reduceMotion = Boolean(useReducedMotion())
  const failed = item.status === 'failed'
  const cancelled = item.status === 'cancelled'
  const terminal = failed || cancelled
  const isDock = presentation === 'dock'
  const [isExpanded, setIsExpanded] = useState(isDock || terminal)
  const steps = item.progress.steps
  const activeStep = steps.find((step) => step.status === 'running') ?? steps[steps.length - 1] ?? null
  const activeStatus = knowledgeRunHeaderStatus({
    collectionCount,
    fallback: t.knowledge.runPreparing,
    step: activeStep,
    t: t.knowledge,
  })
  const facts = knowledgeRunFacts({ collectionCount, item, t: t.knowledge })

  return (
    <motion.article
      animate={{ clipPath: 'inset(0% 0% 0% 0%)', opacity: 1, y: 0 }}
      className={cn(
        'relative overflow-hidden border bg-card shadow-[0_1px_2px_var(--shadow-hairline)]',
        isDock
          ? 'inqtrix-rag-island rounded-b-none rounded-t-xl border-b-0 border-brand/25 px-3 pb-3 pt-3 shadow-[0_18px_42px_-28px_var(--brand)] md:px-4'
          : 'rounded-lg border-border p-4',
        terminal && 'border-destructive/35',
      )}
      initial={reduceMotion ? false : {
        clipPath: isDock ? 'inset(18% 0% 0% 0%)' : 'inset(0% 0% 0% 0%)',
        opacity: 0,
        y: isDock ? 12 : 6,
      }}
      layout={isDock}
      style={isDock ? { transformOrigin: 'bottom center' } : undefined}
      transition={appMotion.card}
    >
      <div className="relative z-10 flex min-w-0 items-start gap-3">
        <LiveGlyph cancelled={cancelled} failed={failed} reduceMotion={reduceMotion} />
        <div className="min-w-0 flex-1">
          <div className="flex min-w-0 flex-col gap-2 md:flex-row md:items-start md:justify-between">
            <div className="min-w-0">
              <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
                <h3 className={cn('truncate t-section', terminal ? 'text-destructive' : 'text-foreground')}>
                  {cancelled ? t.knowledge.runCancelled : failed ? t.knowledge.runFailed : t.knowledge.runTitle}
                </h3>
                {!terminal && (
                  <span className="inline-flex shrink-0 items-center gap-1 t-hint font-semibold text-brand">
                    <span
                      aria-hidden="true"
                      className={cn('size-1.5 rounded-full bg-brand', !reduceMotion && 'inqtrix-running-dot')}
                    />
                    {t.knowledge.runLive}
                  </span>
                )}
              </div>
              <p
                className="mt-0.5 flex min-w-0 items-center gap-1 t-meta text-muted-foreground"
                title={activeStatus.title}
              >
                <span className="shrink-0 font-semibold text-foreground/80">{t.knowledge.runCurrent}</span>
                <span className="shrink-0">·</span>
                <span className="min-w-0 truncate">{activeStatus.value}</span>
              </p>
            </div>
            <div className="grid min-w-0 grid-cols-2 gap-x-4 gap-y-1.5 md:max-w-[54%] md:grid-cols-4 md:justify-items-end">
              {facts.map((fact) => (
                <span
                  className="grid min-w-0 gap-0.5 border-l border-border/70 pl-2.5 md:pl-3"
                  data-knowledge-run-fact={fact.id}
                  key={fact.id}
                >
                  <span className="truncate t-hint text-muted-foreground">{fact.label}</span>
                  <span
                    className={cn(
                      'truncate t-list tabular-nums',
                      fact.pending ? 'text-muted-foreground/60' : 'text-foreground/90',
                    )}
                  >
                    {fact.value}
                  </span>
                </span>
              ))}
            </div>
          </div>
        </div>
        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              aria-expanded={isExpanded}
              aria-label={isExpanded ? t.knowledge.runCollapse : t.knowledge.runExpand}
              className="size-7 shrink-0 text-muted-foreground hover:text-foreground"
              onClick={() => setIsExpanded((current) => !current)}
              size="icon"
              type="button"
              variant="ghost"
            >
              <ChevronDown
                className={cn('size-4 transition-transform duration-300', !isExpanded && '-rotate-90')}
              />
            </Button>
          </TooltipTrigger>
          <TooltipContent>
            {isExpanded ? t.knowledge.runCollapse : t.knowledge.runExpand}
          </TooltipContent>
        </Tooltip>
      </div>

      <div
        className={cn(
          'relative z-10 grid overflow-hidden transition-[grid-template-rows] duration-300 ease-out motion-reduce:transition-none',
          isExpanded ? 'grid-rows-[1fr]' : 'grid-rows-[0fr]',
        )}
      >
        <div className="min-h-0 overflow-hidden">
          <div
            className={cn(
              'mt-3',
              isDock
                ? 'border-t border-border/70 pt-2'
                : 'rounded-md border border-border/70 bg-background/45 p-2.5',
            )}
          >
            <KnowledgeStepList
              collectionCount={collectionCount}
              failed={failed}
              steps={steps}
              variant={isDock ? 'live' : 'default'}
            />
          </div>
          {failed && item.error && (
            <p className="mt-2 t-meta text-destructive">{item.error}</p>
          )}
        </div>
      </div>
    </motion.article>
  )
}

function LiveGlyph({
  cancelled,
  failed,
  reduceMotion,
}: {
  cancelled: boolean
  failed: boolean
  reduceMotion: boolean
}) {
  if (failed || cancelled) {
    return (
      <span className="grid size-8 shrink-0 place-items-center rounded-full border border-destructive/30 bg-background text-destructive">
        {cancelled ? <Square className="size-3 fill-current" /> : <AlertTriangle className="size-4" />}
      </span>
    )
  }

  return (
    <span className="relative grid size-8 shrink-0 place-items-center rounded-full border border-brand/25 bg-brand-subtle/55 text-brand">
      {!reduceMotion && (
        <>
          <span className="inqtrix-active-node-ring absolute inset-1 rounded-full border border-brand/35" />
          <span className="inqtrix-active-node-ring inqtrix-active-node-ring-delayed absolute inset-1 rounded-full border border-brand/25" />
        </>
      )}
      <Sparkles className={cn('relative z-10 size-4', !reduceMotion && 'inqtrix-active-node-core')} />
    </span>
  )
}
