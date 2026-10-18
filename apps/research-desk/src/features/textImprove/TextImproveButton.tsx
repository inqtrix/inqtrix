import { LoaderCircle, Sparkles } from '@/components/icons'
import { Button } from '@/components/ui/button'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { cn } from '@/lib/utils'
import { motion } from 'motion/react'

export function TextImproveButton({
  className,
  disabled,
  isLoading,
  label,
  loadingLabel,
  onClick,
  reduceMotion,
}: {
  className?: string
  disabled?: boolean
  isLoading: boolean
  label: string
  loadingLabel: string
  onClick: () => void
  reduceMotion: boolean | null
}) {
  const currentLabel = isLoading ? loadingLabel : label

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button
          aria-label={currentLabel}
          className={cn(
            'size-7 text-muted-foreground hover:text-brand focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring',
            isLoading && 'text-brand',
            className,
          )}
          disabled={disabled || isLoading}
          onClick={onClick}
          size="icon"
          type="button"
          variant="ghost"
        >
          <motion.span
            animate={
              isLoading && !reduceMotion
                ? { rotate: 20, scale: [1, 1.08, 1] }
                : { rotate: 0, scale: 1 }
            }
            className="inline-flex"
            transition={{ duration: 0.7, ease: [0.22, 1, 0.36, 1], repeat: isLoading && !reduceMotion ? Infinity : 0 }}
          >
            {isLoading ? (
              <LoaderCircle className="size-3.5 animate-spin" />
            ) : (
              <Sparkles className="size-3.5" />
            )}
          </motion.span>
        </Button>
      </TooltipTrigger>
      <TooltipContent>{currentLabel}</TooltipContent>
    </Tooltip>
  )
}
