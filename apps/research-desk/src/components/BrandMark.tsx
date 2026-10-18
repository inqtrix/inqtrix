import flatLight from '@/brand/inqtrix-mark-flat-light-128.png'
import flatDark from '@/brand/inqtrix-mark-flat-dark-128.png'
import matteLight from '@/brand/inqtrix-mark-lightmode-128.png'
import matteDark from '@/brand/inqtrix-mark-darkmode-128.png'
import monoLight from '@/brand/inqtrix-mark-mono-navy-128.png'
import monoDark from '@/brand/inqtrix-mark-mono-white-128.png'
import { cn } from '@/lib/utils'

type BrandMarkProps = {
  className?: string
  variant?: 'flat' | 'matte'
}

/** The Inqtrix mark, with fixed brand colors and root-driven contrast.
 * Decorative: the adjacent wordmark or heading supplies the accessible name.
 * CSS uses the prepaint theme attributes, so no provider or JS image swap is needed.
 */
export function BrandMark({ className, variant = 'flat' }: BrandMarkProps) {
  return (
    <span aria-hidden="true" className={cn('inqtrix-brand-mark', className)}>
      <img alt="" className="inqtrix-brand-mark-light" draggable={false} height={128} src={variant === 'matte' ? matteLight : flatLight} width={128} />
      <img alt="" className="inqtrix-brand-mark-dark" draggable={false} height={128} src={variant === 'matte' ? matteDark : flatDark} width={128} />
      <img alt="" className="inqtrix-brand-mark-mono-light" draggable={false} height={128} src={monoLight} width={128} />
      <img alt="" className="inqtrix-brand-mark-mono-dark" draggable={false} height={128} src={monoDark} width={128} />
    </span>
  )
}
