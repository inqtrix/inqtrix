import { type ReactNode } from 'react'
import { BrandMark } from '@/components/BrandMark'
import { cn } from '@/lib/utils'

type WelcomeStateProps = {
  actions?: ReactNode
  body?: ReactNode
  className?: string
  example?: ReactNode
  kicker?: string
  subtitle: ReactNode
  title: string
}

export function WelcomeState({
  actions,
  body,
  className,
  example,
  kicker,
  subtitle,
  title,
}: WelcomeStateProps) {
  return (
    <div className={cn('mx-auto flex w-full max-w-xl flex-col items-center text-center', className)}>
      <div className="flex size-10 items-center justify-center rounded-xl border border-border bg-surface text-brand shadow-[0_1px_2px_var(--shadow-hairline)]">
        <BrandMark className="size-5" />
      </div>
      {kicker ? (
        <p className="mt-4 t-caption text-muted-foreground/65">{kicker}</p>
      ) : null}
      <h2 className={cn('t-display text-foreground', kicker ? 'mt-1.5' : 'mt-4')}>
        {title}
      </h2>
      <p className="mt-1.5 max-w-lg t-meta text-muted-foreground">
        {subtitle}
      </p>
      {body ? (
        <div className="mt-3 flex max-w-lg flex-col gap-2 t-meta text-muted-foreground">
          {body}
        </div>
      ) : null}
      {example ? (
        <p className="mt-3 max-w-lg t-meta-sm text-muted-foreground/85">
          {example}
        </p>
      ) : null}
      {actions ? <div className="mt-5">{actions}</div> : null}
    </div>
  )
}
