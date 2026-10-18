import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import { BookOpen } from '@/components/icons'
import {
  MentionMenu,
  type MentionMenuOption,
  type MentionMenuScope,
} from './mention-menu'

const labels = {
  backHint: 'Zurück',
  closeHint: 'Schließen',
  filterPlaceholder: 'Filtern…',
  navHint: 'Navigieren',
  rootTitle: 'Kontext hinzufügen',
  selectHint: 'Auswählen',
}

const ruleScope: MentionMenuScope = { icon: BookOpen, kind: 'Regeln', query: '', tone: 'success' }

function option(overrides: Partial<MentionMenuOption>): MentionMenuOption {
  return {
    icon: BookOpen,
    isCategory: false,
    primary: 'Executive Briefing',
    secondary: '@rules:briefing',
    tone: 'success',
    ...overrides,
  }
}

function menu(scope: MentionMenuScope, options: MentionMenuOption[], activeIndex = 0): string {
  return renderToStaticMarkup(
    <MentionMenu
      activeIndex={activeIndex}
      labels={labels}
      onBack={() => undefined}
      onHover={() => undefined}
      onSelect={() => undefined}
      options={options}
      scope={scope}
    />,
  )
}

/** The attributes of the one span whose whole text is `text`. */
function spanAttributes(html: string, text: string): string {
  const escaped = text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  return html.match(new RegExp(`<span([^>]*)>${escaped}</span>`))?.[1] ?? ''
}

/** The row button that carries `primary`. */
function row(html: string, primary: string): string {
  return (html.match(/<button[^>]*>[\s\S]*?<\/button>/g) ?? [])
    .find((button) => button.includes(`>${primary}</span>`)) ?? ''
}

describe('the scope header', () => {
  // The rule scope says what it cannot pre-check on the right of its
  // header. With `shrink-0` that sentence overflowed and the scope chip
  // ("Regeln") was drawn on top of it.
  const longValue = 'Die Größe prüft der Server beim Senden · Die Anzahl prüft der Server beim Senden'

  it('lets a long value give way, cut with its full text reachable', () => {
    const value = spanAttributes(menu({ ...ruleScope, value: longValue }, [option({})]), longValue)
    expect(value).not.toBe('')
    expect(value).toContain(`title="${longValue}"`)
    expect(value).toMatch(/\btruncate\b/)
    // Only the space the scope label leaves: never wider than that.
    expect(value).toMatch(/\bflex-1\b/)
    expect(value).not.toMatch(/\bshrink-0\b/)
    // A sentence, not a count: readable contrast.
    expect(value).toMatch(/text-muted-foreground(?![/\w-])/)
  })

  it('keeps the scope label at its own width', () => {
    // The value takes what the label leaves; the label chip itself never
    // gives way, so nothing can be drawn over it.
    const html = menu({ ...ruleScope, value: longValue }, [option({})])
    const chip = html.match(/<span class="([^"]*)"><svg[^>]*lucide-book-open[^>]*>[\s\S]*?<\/svg>Regeln<\/span>/)?.[1] ?? ''
    expect(chip).not.toBe('')
    expect(chip).toMatch(/\bshrink-0\b/)
  })

  it('keeps the count as it was', () => {
    const count = spanAttributes(menu(ruleScope, [option({})]), '1')
    expect(count).toMatch(/\bshrink-0\b/)
    expect(count).toContain('text-muted-foreground/50')
    expect(count).not.toContain('title=')
  })
})

describe('a row that cannot be picked or states the situation', () => {
  const reason = 'Regeln gelten für neue Aufträge — bei einer offenen Rückfrage nicht anhängbar'

  it('wraps a blocked entry instead of cutting its reason', () => {
    const html = menu(ruleScope, [option({ disabled: true, secondary: reason })])
    const reasonSpan = spanAttributes(html, reason)
    expect(reasonSpan).not.toBe('')
    expect(reasonSpan).not.toMatch(/\btruncate\b/)
    expect(reasonSpan).toMatch(/\bwhitespace-normal\b/)
    expect(spanAttributes(html, 'Executive Briefing')).not.toMatch(/\btruncate\b/)
  })

  it('wraps a state row, with or without an action', () => {
    for (const disabled of [true, false]) {
      const html = menu(ruleScope, [option({ disabled, primary: reason, secondary: 'Regeln', state: true })])
      expect(spanAttributes(html, reason)).toMatch(/\bwhitespace-normal\b/)
      expect(spanAttributes(html, reason)).not.toMatch(/\btruncate\b/)
    }
  })

  it('marks a state row without an action as such, with no Enter hint', () => {
    const html = menu(ruleScope, [option({ disabled: true, primary: reason, secondary: 'Regeln', state: true })])
    const button = row(html, reason)
    expect(button).toContain('aria-disabled="true"')
    expect(button).not.toContain('<kbd')
    // A sentence about the situation stays readable — not dimmed like an
    // entry that cannot be picked.
    expect(button).not.toMatch(/\bopacity-50\b/)
  })

  it('offers Enter on a state row that has an action', () => {
    const html = menu(ruleScope, [option({ primary: 'Alle freigegebenen Regeln sind angehängt', state: true })])
    const button = row(html, 'Alle freigegebenen Regeln sind angehängt')
    expect(button).not.toContain('aria-disabled')
    expect(button).toContain('<kbd')
  })

  it('reads a category row note in full', () => {
    // The root row of a blocked category says why in its note; cut there,
    // the reason could not be read anywhere before stepping in.
    const html = menu({ kind: null, query: '' }, [option({ isCategory: true, primary: 'Regeln', secondary: reason })])
    expect(spanAttributes(html, reason)).toMatch(/\bwhitespace-normal\b/)
    expect(spanAttributes(html, reason)).not.toMatch(/\btruncate\b/)
  })

  it('keeps a pickable entry on one line', () => {
    const html = menu(ruleScope, [option({})])
    expect(spanAttributes(html, 'Executive Briefing')).toMatch(/\btruncate\b/)
    expect(spanAttributes(html, '@rules:briefing')).toMatch(/\btruncate\b/)
  })
})
