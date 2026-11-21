import type { ReactNode } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'

import { TooltipProvider } from '@/components/ui/tooltip'
import { translations } from '@/i18n/translations'
import { SkillInvocationField, SkillLibraryPanel } from './SkillLibraryPanel'
import { SKILL_ALLOWED_TOOL_OPTIONS, type SkillInfo } from './skillLibrary'
import type { SkillsApiHandle } from './useSkillsApi'

vi.mock('@/i18n/LocaleProvider', () => ({
  useLocale: () => ({ locale: 'de', t: translations.de }),
}))

// The list scroller measures its viewport in an effect; the markup of
// the editor is what these tests read.
vi.mock('@/components/ui/scroll-area', () => ({
  ScrollArea: ({ children }: { children?: ReactNode }) => <div>{children}</div>,
}))

const s = translations.de.skills

function skill(access: SkillInfo['access']): SkillInfo {
  return {
    id: 'skill_1',
    label: 'mail',
    title: 'Mail',
    description: '',
    when_to_use: '',
    instructions_markdown: 'Write.',
    clarification_points: [],
    deliverable: '',
    allowed_tools: [],
    requires_plan: 'auto',
    invocation: 'user_only',
    argument_hint: '',
    model_tier: '',
    effort: '',
    include_in_autocomplete: true,
    revision: 1,
    created_at: 0,
    updated_at: 0,
    access,
  }
}

const OWNER = { mode: 'owner' } as SkillInfo['access']
const EDITOR = { mode: 'shared', permission: 'edit' } as SkillInfo['access']

function invocationField(props: { editable: boolean; skill: SkillInfo | null }) {
  return renderToStaticMarkup(
    <SkillInvocationField
      editable={props.editable}
      id="invocation"
      onChange={() => {}}
      skill={props.skill}
      value="user_only"
    />,
  )
}

/** The opening tag of every element of one kind, attributes included. */
function openingTags(html: string, tag: string): string[] {
  return html.match(new RegExp(`<${tag}\\b[^>]*>`, 'g')) ?? []
}

function isDisabled(openingTag: string): boolean {
  return /\sdisabled(=""|\s|>)/.test(openingTag)
}

describe('the field that says who may activate a skill', () => {
  it('is locked for someone who edits a shared skill, and says why', () => {
    // The edit permission covers the text. Who may activate the skill is
    // the owner's choice, so the form must not offer it to an editor.
    const html = invocationField({ editable: true, skill: skill(EDITOR) })
    const [select] = openingTags(html, 'select')
    expect(isDisabled(select)).toBe(true)
    expect(html).toContain(s.fieldInvocationOwnerOnlyHint)
    expect(html).not.toContain(s.fieldInvocationHint)
  })

  it('stays open for the owner and for a new skill', () => {
    for (const subject of [skill(OWNER), null]) {
      const html = invocationField({ editable: true, skill: subject })
      const [select] = openingTags(html, 'select')
      expect(isDisabled(select)).toBe(false)
      expect(html).toContain(s.fieldInvocationHint)
      expect(html).not.toContain(s.fieldInvocationOwnerOnlyHint)
    }
  })

  it('is locked with the rest of the form when the form is read-only', () => {
    const html = invocationField({ editable: false, skill: skill(OWNER) })
    expect(isDisabled(openingTags(html, 'select')[0])).toBe(true)
    // The owner sees the lock of the whole form, not the owner-only reason.
    expect(html).toContain(s.fieldInvocationHint)
  })
})

describe('locked controls of the skill editor', () => {
  const api: SkillsApiHandle = {
    skills: [],
    loading: false,
    error: '',
    writable: true,
    transferEnabled: true,
    refresh: async () => {},
    create: async () => null,
    update: async () => null,
    remove: async () => false,
    exportMarkdown: async () => null,
    importMarkdown: async () => null,
  }
  const html = renderToStaticMarkup(
    <TooltipProvider>
      <SkillLibraryPanel api={api} />
    </TooltipProvider>,
  )

  it('look locked: every native select dims like the shared form controls', () => {
    // A disabled select without this renders exactly like an open one;
    // a click on it then does nothing without a visible reason.
    const selects = openingTags(html, 'select')
    expect(selects).toHaveLength(5)
    for (const select of selects) {
      expect(select).toContain('disabled:opacity-50')
      expect(select).toContain('disabled:cursor-not-allowed')
    }
  })

  it('look locked: every tool chip dims like the shared buttons', () => {
    const chips = openingTags(html, 'button').filter((button) =>
      button.includes('t-meta') && button.includes('rounded-md border'))
    expect(chips).toHaveLength(SKILL_ALLOWED_TOOL_OPTIONS.length)
    for (const chip of chips) {
      expect(chip).toContain('disabled:opacity-50')
      expect(chip).toContain('disabled:pointer-events-none')
    }
  })
})
