import type { ResourceAccess } from '@/features/sharing/types'

/**
 * Skill library domain logic: the wire types of
 * `/v1/skills` and the pure helpers behind the editor — placeholder
 * extraction (the FE twin of the server's `extract_placeholders`,
 * same regex, so scaffolding and the save validation can never
 * disagree) and the point scaffolding that keeps declared points
 * coupled to the `{{name}}` slots in the instructions.
 */

export type SkillAccess = ResourceAccess

/** One clarification point as stored/served (sanitized ids server-side). */
export type SkillPointInfo = {
  id?: string
  name: string
  question: string
  options: { id?: string; label: string; description?: string }[]
  required: boolean
  default_assumption: string
}

/** The `/v1/skills` wire record. */
export type SkillInfo = {
  id: string
  label: string
  title: string
  description: string
  when_to_use: string
  instructions_markdown: string
  clarification_points: SkillPointInfo[]
  deliverable: '' | 'chat' | 'canvas' | 'email' | 'talking_points'
  allowed_tools: string[]
  requires_plan: 'always' | 'auto' | 'never'
  invocation: 'user_only' | 'model_allowed'
  argument_hint: string
  model_tier: '' | 'high' | 'mid' | 'fast'
  effort: string
  include_in_autocomplete: boolean
  revision: number
  created_at: number
  updated_at: number
  access: SkillAccess
}

/** The writable fields (create/update body). */
export type SkillPayload = Omit<
  SkillInfo,
  'id' | 'created_at' | 'updated_at' | 'revision' | 'access'
>

/** Kernel tool names a skill may allow — the author-facing vocabulary
 * the backend enforces (kernel guard + planner task-kind mapping). */
/** Server twins of content/skills.py MAX_CLARIFICATION_POINTS /
 * MAX_POINT_OPTIONS — the editor caps VISIBLY at the same numbers the
 * validator enforces. */
export const MAX_SKILL_POINTS = 5
export const MAX_POINT_OPTIONS = 4

export const SKILL_ALLOWED_TOOL_OPTIONS = [
  'search_context',
  'read_context',
  'search_project_knowledge',
  'read_project_document',
  'web_instant',
  'run_web_research',
  'run_deep_mission',
  'write_canvas',
  'read_editor_document',
  'search_editor_document',
  'propose_editor_patch',
] as const

const PLACEHOLDER_PATTERN = /\{\{\s*([a-zA-Z0-9_-]+)\s*\}\}/g

/** The ordered, de-duplicated `{{name}}` tokens of a skill body. */
export function extractPlaceholders(instructionsMarkdown: string): string[] {
  const seen: string[] = []
  for (const match of instructionsMarkdown.matchAll(PLACEHOLDER_PATTERN)) {
    if (!seen.includes(match[1])) seen.push(match[1])
  }
  return seen
}

export function emptySkillPoint(name = ''): SkillPointInfo {
  return {
    name,
    question: '',
    options: [],
    required: false,
    default_assumption: '',
  }
}

/**
 * Keep the point list coupled to the instructions (plan `3.4`): every
 * placeholder gets a row (existing rows keep their content), rows for
 * REMOVED placeholders stay — they may be intentional context-only
 * points, deleting authored content on a text edit would be data loss.
 * Order: placeholder rows in text order first, then the free rows.
 */
export function scaffoldPoints(
  instructionsMarkdown: string,
  points: SkillPointInfo[],
): SkillPointInfo[] {
  const placeholders = extractPlaceholders(instructionsMarkdown)
  const byName = new Map(
    points.filter((point) => point.name).map((point) => [point.name, point]),
  )
  const placeholderRows = placeholders.map(
    (name) => byName.get(name) ?? emptySkillPoint(name),
  )
  const freeRows = points.filter(
    (point) => !point.name || !placeholders.includes(point.name),
  )
  return [...placeholderRows, ...freeRows]
}

/** Placeholders that would fail the server's coupling rule (400). */
export function uncoveredPlaceholders(
  instructionsMarkdown: string,
  points: SkillPointInfo[],
): string[] {
  const names = new Set(
    points
      .filter((point) => point.name && point.question.trim())
      .map((point) => point.name),
  )
  return extractPlaceholders(instructionsMarkdown).filter(
    (name) => !names.has(name),
  )
}

export function emptySkillPayload(): SkillPayload {
  return {
    label: '',
    title: '',
    description: '',
    when_to_use: '',
    instructions_markdown: '',
    clarification_points: [],
    deliverable: '',
    allowed_tools: [],
    requires_plan: 'auto',
    invocation: 'user_only',
    argument_hint: '',
    model_tier: '',
    effort: '',
    include_in_autocomplete: true,
  }
}

export function payloadFromSkill(skill: SkillInfo): SkillPayload {
  return {
    label: skill.label,
    title: skill.title,
    description: skill.description,
    when_to_use: skill.when_to_use,
    instructions_markdown: skill.instructions_markdown,
    clarification_points: skill.clarification_points.map((point) => ({
      name: point.name,
      question: point.question,
      options: point.options.map((option) => ({
        label: option.label,
        description: option.description ?? '',
      })),
      required: point.required,
      default_assumption: point.default_assumption,
    })),
    deliverable: skill.deliverable,
    allowed_tools: [...skill.allowed_tools],
    requires_plan: skill.requires_plan,
    invocation: skill.invocation,
    argument_hint: skill.argument_hint,
    model_tier: skill.model_tier,
    effort: skill.effort,
    include_in_autocomplete: skill.include_in_autocomplete,
  }
}

/** Owner or edit-grant may save; view-grant and unknown stay read-only. */
export function canEditSkill(skill: SkillInfo | null): boolean {
  if (!skill) return true
  return skill.access.mode !== 'shared' || skill.access.permission !== 'view'
}

/** Deletion stays owner-only (shares never delete). */
export function canDeleteSkill(skill: SkillInfo | null): boolean {
  return skill != null && skill.access.mode !== 'shared'
}
