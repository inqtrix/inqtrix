import { useLocale } from '@/i18n/LocaleProvider'
import { cn } from '@/lib/utils'

/**
 * Standing line under a prompt composer stating that answers come from an AI
 * system and asking the reader to verify them.
 *
 * It lives at the composer rather than in the workspace empty state because
 * the empty state is conditional on having no data: anyone opening a project
 * that already holds runs, threads, or sessions never sees it, and would
 * never be told. The composer is present in every AI workspace in every
 * state, so this is the one surface that always reaches the person about to
 * type.
 *
 * Deliberately permanent, not hidden while typing: most composers have no
 * reserved row below the box, so toggling it would shift the input by a
 * line height on the first keystroke — more noticeable than the line itself.
 *
 * The composers are independent implementations with no shared shell, so
 * this leaf exists to keep one styling owner for every insertion instead of
 * a copy in each. `t-meta` is the role for helper text; the colour
 * token stays at full opacity because opacity modifiers are reserved for
 * decorative glyphs and disabled content, and dimmed metadata loses its
 * readable contrast. It deliberately does NOT drop to `t-hint`:
 * that role is one step below the context meter sitting beside it, and the
 * Commission's Article 50 guidance rejects a disclosure buried in fine
 * print. The composer band carries `pb-2` instead of `pb-4` so this line
 * costs the input only a few pixels of height.
 */
export function ComposerDisclosureHint({ className }: { className?: string }) {
  const { t } = useLocale()

  return (
    <p className={cn('mt-1 px-1 t-meta text-muted-foreground', className)}>
      {t.aiTransparency.firstInteraction}
    </p>
  )
}
