import { renderToStaticMarkup } from 'react-dom/server'
import type { NodeViewProps } from '@tiptap/react'
import { describe, expect, it, vi } from 'vitest'

import { translations } from '@/i18n/translations'

import type { CommandPillKind } from './CommandPill'
import { CommandPillView } from './CommandPillView'
import {
  CommandPillStateContext,
  NO_COMMAND_PILL_STATE,
  type CommandPillState,
} from './commandPillState'

const localeState = vi.hoisted(() => ({ locale: 'de' as 'de' | 'en' }))

vi.mock('@/i18n/LocaleProvider', () => ({
  useLocale: () => ({ locale: localeState.locale, t: translations[localeState.locale] }),
}))

function pill(
  kind: CommandPillKind,
  label: string,
  state: CommandPillState = NO_COMMAND_PILL_STATE,
  id = 'id-1',
): string {
  const props = { node: { attrs: { pillId: id, pillKind: kind, pillLabel: label } } } as unknown as NodeViewProps
  return renderToStaticMarkup(
    <CommandPillStateContext.Provider value={state}>
      <CommandPillView {...props} />
    </CommandPillStateContext.Provider>,
  )
}

const ariaLabel = (html: string) => html.match(/aria-label="([^"]*)"/)?.[1] ?? null

describe('a command pill names itself for a screen reader', () => {
  // Inside the textbox a pill is read as its bare title: a rule pill and a
  // typed word sounded the same, and nothing said how to remove it.
  it('says what a rule pill is and how to remove it', () => {
    expect(ariaLabel(pill('rule', 'Executive Briefing')))
      .toBe('Regel: Executive Briefing, mit Rücktaste entfernen')
  })

  it('names every kind for what it is', () => {
    expect(ariaLabel(pill('skill', 'memo'))).toBe('Skill: memo, mit Rücktaste entfernen')
    expect(ariaLabel(pill('knowledge', 'Wissen')))
      .toBe('Quelle: Wissen, mit Rücktaste ausschalten')
    expect(ariaLabel(pill('responseForm', 'Canvas')))
      .toBe('Antwortform: Canvas, mit Rücktaste zurücksetzen')
  })

  it('gives the name a role that may carry it and hides the glyph', () => {
    // A generic span may not carry a name; an atom with role img may, and
    // its glyph and visible text are then read once, as that name.
    const html = pill('rule', 'Executive Briefing')
    const span = html.match(/<span[^>]*data-command-pill-kind="rule"[^>]*>/)?.[0] ?? ''
    expect(span).toContain('role="img"')
    expect(span).toContain('aria-label="Regel: Executive Briefing, mit Rücktaste entfernen"')
    expect(html).toMatch(/<svg[^>]*aria-hidden="true"/)
  })

  it('speaks English for an English desk', () => {
    localeState.locale = 'en'
    try {
      expect(ariaLabel(pill('rule', 'Executive Briefing')))
        .toBe('Rule: Executive Briefing, remove with Backspace')
      expect(ariaLabel(pill('skill', 'memo'))).toBe('Skill: memo, remove with Backspace')
    } finally {
      localeState.locale = 'de'
    }
  })
})

describe('a document capsule says which document it is and in what state', () => {
  const longTitle = 'Gibt es ein Tool für einen digitalen Kleiderschrank, was sind die aktuellen Entwicklungen auf diesem Gebiet?'
  const outer = (html: string) => html.match(/<span[^>]*data-command-pill-kind="[^"]*"[^>]*>/)?.[0] ?? ''

  it('carries the report icon the chat uses, not the rule icon', () => {
    // Chat: BookOpen = rule, FileText = research report. One symbol must
    // not mean two things on the same desk.
    const html = pill('report', 'CO2-Flottengrenzwerte')
    expect(html).toContain('lucide-file-text')
    expect(html).not.toContain('lucide-book-open')
  })

  it('names a report, a canvas document and the editing target', () => {
    expect(ariaLabel(pill('report', 'CO2-Bericht')))
      .toBe('Recherche-Bericht: CO2-Bericht, mit Rücktaste entfernen')
    expect(ariaLabel(pill('canvas', 'memo.md'))).toBe('Canvas-Dokument: memo.md, mit Rücktaste entfernen')
    expect(ariaLabel(pill('editTarget', 'Vertragsentwurf')))
      .toBe('Bearbeitungsziel: Vertragsentwurf, mit Rücktaste entfernen')
  })

  it('shortens a long title visibly and keeps the whole title reachable', () => {
    const html = pill('report', longTitle)
    // The title shows with an ellipsis, never cut silently: the tooltip and
    // the screen reader carry all of it.
    expect(html).toMatch(/<span[^>]*class="[^"]*\btruncate\b[^"]*"[^>]*>Gibt es ein Tool/)
    expect(outer(html)).toContain(`title="${longTitle}"`)
    expect(ariaLabel(html)).toBe(`Recherche-Bericht: ${longTitle}, mit Rücktaste entfernen`)
  })

  it('marks a capsule whose document is gone instead of showing a raw id', () => {
    const html = pill('report', 'CO2-Bericht', { refusedKinds: [], unavailable: ['report:r9'] }, 'r9')
    expect(outer(html)).toContain('data-command-pill-unavailable="true"')
    expect(outer(html)).toContain('text-warning')
    expect(html).toContain('nicht mehr verfügbar')
    expect(ariaLabel(html))
      .toBe('Recherche-Bericht: CO2-Bericht (nicht mehr verfügbar), mit Rücktaste entfernen')
    // Another capsule of the same kind is unaffected.
    expect(outer(pill('report', 'Marktbericht', { refusedKinds: [], unavailable: ['report:r9'] }, 'r1')))
      .not.toContain('data-command-pill-unavailable')
  })

  it('carries the engine refusal on the capsule, described by the notice', () => {
    const html = pill('report', 'CO2-Bericht', {
      refusalNoticeId: 'refusal-1',
      refusedKinds: ['report', 'canvas'],
      unavailable: [],
    })
    expect(outer(html)).toContain('data-engine-refused="true"')
    expect(outer(html)).toContain('aria-describedby="refusal-1"')
    expect(outer(html)).toContain('text-warning')
    // A rule is no attachment the engine refuses.
    expect(outer(pill('rule', 'Executive Briefing', {
      refusalNoticeId: 'refusal-1',
      refusedKinds: ['report', 'canvas'],
      unavailable: [],
    }))).not.toContain('data-engine-refused')
  })

  it('speaks English for an English desk', () => {
    localeState.locale = 'en'
    try {
      expect(ariaLabel(pill('report', 'CO2 report'))).toBe('Research report: CO2 report, remove with Backspace')
      expect(ariaLabel(pill('editTarget', 'Contract', { refusedKinds: [], unavailable: ['editTarget:id-1'] })))
        .toBe('Editing target: Contract (no longer available), remove with Backspace')
    } finally {
      localeState.locale = 'de'
    }
  })
})
