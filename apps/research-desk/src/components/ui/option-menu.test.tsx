import type { ReactNode } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'

import { BookOpen } from '@/components/icons'
import { OptionMenuItem } from './option-menu'

// The real item needs an open Radix menu (a portal); inline it so the
// static markup carries the row.
vi.mock('@/components/ui/dropdown-menu', () => ({
  DropdownMenuItem: ({ children }: { children?: ReactNode }) => <div>{children}</div>,
}))

const reason = 'Nicht anhängbar: verknüpfte Dateien gibt der Agent Desk nicht mit'

function item(disabled: boolean): string {
  return renderToStaticMarkup(
    <OptionMenuItem
      active={false}
      description={reason}
      disabled={disabled}
      icon={BookOpen}
      label="Kontextpaket Vertrieb"
      onSelect={() => undefined}
    />,
  )
}

const classOf = (html: string, text: string) =>
  html.match(new RegExp(`<span class="([^"]*)">${text}</span>`))?.[1] ?? ''

describe('an option that cannot be picked', () => {
  // The same rows as the "@" menu: a reason cut with an ellipsis is a
  // reason nobody can read.
  it('wraps its reason instead of cutting it', () => {
    const html = item(true)
    expect(classOf(html, reason)).toMatch(/\bwhitespace-normal\b/)
    expect(classOf(html, reason)).not.toMatch(/\btruncate\b/)
    expect(classOf(html, 'Kontextpaket Vertrieb')).not.toMatch(/\btruncate\b/)
  })

  it('keeps a pickable option on one line', () => {
    const html = item(false)
    expect(classOf(html, reason)).toMatch(/\btruncate\b/)
    expect(classOf(html, 'Kontextpaket Vertrieb')).toMatch(/\btruncate\b/)
  })
})
