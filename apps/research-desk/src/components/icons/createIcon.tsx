import {
  forwardRef,
  type ForwardRefExoticComponent,
  type ReactNode,
  type RefAttributes,
  type SVGProps,
} from 'react'

export type IconProps = SVGProps<SVGSVGElement> & {
  absoluteStrokeWidth?: boolean
  color?: string
  size?: number | string
  strokeWidth?: number | string
}

export type LucideIcon = ForwardRefExoticComponent<
  IconProps & RefAttributes<SVGSVGElement>
>

const accessibilityProps = [
  'aria-label',
  'aria-labelledby',
  'aria-hidden',
  'title',
] as const

export function createIcon(
  displayName: string,
  iconClassName: string,
  iconNode: ReactNode,
): LucideIcon {
  const Icon = forwardRef<SVGSVGElement, IconProps>(
    (
      {
        absoluteStrokeWidth,
        children,
        className,
        color = 'currentColor',
        fill = 'none',
        size = 24,
        strokeWidth = 2.25,
        ...props
      },
      ref,
    ) => {
      const computedStrokeWidth = absoluteStrokeWidth
        ? (Number(strokeWidth) * 24) / Number(size)
        : strokeWidth
      const propsByName = props as Record<string, unknown>
      const hasA11yProp = accessibilityProps.some((prop) => propsByName[prop] != null)

      return (
        <svg
          ref={ref}
          xmlns="http://www.w3.org/2000/svg"
          width={size}
          height={size}
          viewBox="0 0 24 24"
          fill={fill}
          stroke={color}
          strokeWidth={computedStrokeWidth}
          strokeLinecap="round"
          strokeLinejoin="round"
          className={joinClassNames('lucide', iconClassName, className)}
          {...(!children && !hasA11yProp ? { 'aria-hidden': 'true' } : {})}
          {...props}
        >
          {iconNode}
          {children}
        </svg>
      )
    },
  )

  Icon.displayName = displayName
  return Icon
}

function joinClassNames(...classes: Array<string | undefined>) {
  return classes.filter(Boolean).join(' ')
}
