import { describe, expect, it } from 'vitest'

import {
  canChangeSkillInvocation,
  canDeleteSkill,
  canEditSkill,
  type SkillInfo,
} from './skillLibrary'

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

describe('what a viewer may do with a skill', () => {
  it('lets only the owner choose who may activate it', () => {
    // An editor rewrites the text; the owner's model would activate a
    // `model_allowed` skill unasked, so that choice is not the editor's.
    expect(canChangeSkillInvocation(null)).toBe(true)
    expect(canChangeSkillInvocation(skill({ mode: 'owner' } as SkillInfo['access']))).toBe(true)
    expect(canChangeSkillInvocation(skill({ mode: 'unscoped' } as SkillInfo['access']))).toBe(true)
    const editor = skill({ mode: 'shared', permission: 'edit' } as SkillInfo['access'])
    expect(canEditSkill(editor)).toBe(true)
    expect(canChangeSkillInvocation(editor)).toBe(false)
    expect(canDeleteSkill(editor)).toBe(false)
  })
})
