import { useId, useMemo, useRef, useState, useEffect } from 'react'
import { AnimatePresence, motion } from 'motion/react'
import type { FormEvent } from 'react'
import type { JSONContent } from '@tiptap/core'

import {
  BookOpen,
  BrainCircuit,
  Database,
  FileText,
  Globe2,
  Layers,
  Library,
  MessageSquareText,
  PenLine,
  Trash2,
  Plus,
  Gauge,
  Search,
  SendHorizontal,
  Shield,
  ShieldCheck,
  Sparkles,
  WandSparkles,
  Waypoints,
  Workflow,
  X,
} from '@/components/icons'
import { Button } from '@/components/ui/button'
import { Chip } from '@/components/ui/chip'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuSeparator,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { MentionMenu, type MentionMenuOption, type MentionTone } from '@/components/ui/mention-menu'
import {
  OptionMenuHeader,
  OptionMenuItem,
  optionMenuContentClassName,
} from '@/components/ui/option-menu'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { composerIconButtonClassName } from '@/features/composer/ComposerIconButton'
import { ComposerStopButton } from '@/features/composer/ComposerStopButton'
import {
  AgentInlineInput,
  type AgentInlineInputHandle,
  type CommandPillSpec,
} from '@/features/composer/AgentInlineInput'
import type { CommandPillKind } from '@/features/composer/CommandPill'
import {
  CommandPillStateContext,
  commandPillKey,
  type CommandPillState,
} from '@/features/composer/commandPillState'
import {
  detectCollectionMention,
  type CollectionMentionState,
} from '@/features/composer/collectionMention'
import { QuotaMeter } from '@/features/quota/QuotaMeter'
import { ModelTierPicker } from '@/features/researchRuns/ModelTierPicker'
import type {
  AgentTierCapability,
  AgentTierId,
  ChatModelOption,
  ChatModelTier,
  ModelCatalogEntry,
  NodeModelResolution,
} from '@/features/researchRuns/types'
import { effortLevelLabel } from '@/lib/modelCard'
import { useLocale } from '@/i18n/LocaleProvider'
import { countLabel } from '@/i18n/plural'
import { cn } from '@/lib/utils'
import { AgentStatusMenu } from './AgentStatusMenu'
import type {
  AgentEngineMode,
  AgentOverview,
  AgentToolUseCounts,
} from './agentStatusOverview'
import {
  COMPOSER_COMMAND_OPTIONS,
  detectSkillSlash,
  type ComposerCommandId,
  type SkillSlashState,
} from './skillSlash'
import {
  agentContextCategories,
  agentMentionEmptyState,
  agentMentionPick,
  agentMentionRows,
  agentMentionTarget,
  engineRefusesAttachments,
  pickableCount,
  pillKindsKeptAfterSend,
  pillKindsKeptByAnswer,
  ruleIdsForSubmit,
  type AgentContextCategoryId,
  type AgentMentionEmptyState,
  type AgentMentionRow,
  type AgentRuleSource,
} from './contextCategories'
import { agentContextText } from './contextCategoryText'
import {
  extractTrailingComposerCommand,
  knowledgePolicyForScope,
  knowledgeScopeForSubmit,
  knowledgeSwitchState,
  composerCommandEffect,
  webSwitchState,
  type AgentKnowledgeScope,
  type AgentKnowledgeSwitch,
  type AgentExecutionSnapshot,
  type AgentSourcePolicy,
} from './executionPolicy'
import { ComposerDisclosureHint } from '@/features/composer/ComposerDisclosureHint'
import { SourceToggle } from './SourceToggle'
import {
  autonomyModeLabel,
  engineModeLabel,
  responseFormLabel,
  runSetupSummary,
  tierLabel,
} from './runSetupLabels'

export type AgentCollectionOption = { id: string; title: string }

/** The capsule a category's pick becomes in the sentence. A collection
 * stays a chip: it scopes the knowledge search, it is no word of the task. */
const CAPSULE_KIND: Partial<Record<AgentContextCategoryId, CommandPillKind>> = {
  canvas: 'canvas',
  document: 'editTarget',
  report: 'report',
  rule: 'rule',
}

export type AgentDocumentOption = { id: string; title: string }

/** A session canvas document offered for @-mention: pinned as
 * `canvas_context` (no comments) on the next submission. */
export type AgentCanvasDocumentOption = {
  artifactId: string
  revision: number
  /** Derived file name (`marktbericht.md`) — the display identity. */
  name: string
  title: string
}

export type AgentModelPickerProps = {
  /** Concrete-model catalog (empty -> tier fallback inside the picker). */
  catalog: ModelCatalogEntry[]
  options: ChatModelOption[]
  optionsStatus: 'available' | 'missing' | 'unresolved'
  defaultModel: NodeModelResolution | null
  selectedTier: ChatModelTier | null
  selectedModel: string | null
  selectedEffort: string | null
  onTierChange: (tier: ChatModelTier | null) => void
  onModelChange: (model: string | null) => void
  onEffortChange: (effort: string | null) => void
}

export type AgentComposerSubmit = {
  question: string
  autonomy: string
  collectionIds: string[]
  /** Target editor document for a patch assignment; at most one. */
  documentId?: string
  /** The selected engine: the deterministic mission machine or
   * the conversational kernel. Callers gate availability server-side. */
  engineMode: AgentEngineMode
  /** Explicitly attached skill chips; server-admitted. */
  skillIds: string[]
  /** Source availability for this message: web as switched for the
   * session, project knowledge iff a scope was asked for. */
  sourcePolicy: AgentSourcePolicy
  /** What this message asks the server for: attached chips, every visible
   * collection (session switch), or no knowledge at all. */
  knowledgeScope: AgentKnowledgeScope
  /** The session switch as this message leaves it (a trailing `/wissen`
   * switches it on) — the value a session created by this message keeps. */
  knowledgeSwitch: AgentKnowledgeSwitch | undefined
  /** Output form: 'auto' lets the agent decide, 'chat' forces
   * the inline answer, 'canvas' the memo document. */
  responseForm: 'auto' | 'chat' | 'canvas'
  /** Thoroughness: 'deep' = budgets + verification pass. */
  depth: 'normal' | 'deep'
  /** Selected Stufe; null on servers without the tiers capability (the
   * legacy depth toggle applies then). */
  tier: AgentTierId | null
}

export type AgentResponseForm = AgentComposerSubmit['responseForm']

export const AGENT_RESPONSE_FORMS: readonly AgentResponseForm[] = [
  'auto',
  'chat',
  'canvas',
]

/**
 * The Agent Desk composer: context attachments above the textarea, a source
 * dock for persistent availability, one flat execution capsule, and the
 * independent run overview beside quota/send. The draft is caller-owned so it
 * survives view switches; `running` gates only the send, never drafting.
 */
export function AgentComposer({
  answerMode = false,
  autonomy,
  autonomyModes,
  canvasComments = [],
  canvasDocuments = [],
  pinnedCanvasDocumentId = null,
  onPinnedCanvasDocumentChange,
  collections,
  disabled = false,
  documents = [],
  draftDoc = null,
  draftQuestion,
  onDraftDocChange,
  depthMode = 'normal',
  depthSelectable = false,
  tierMode = null,
  tiers = null,
  onTierModeChange,
  engineMode = 'workspace_agent',
  kernelSelectable = false,
  memoryEnabled = false,
  modelPicker = null,
  notice,
  submitError = null,
  onAutonomyChange,
  onDepthModeChange,
  onEngineModeChange,
  onDraftQuestionChange,
  onEditCanvasComment,
  onRemoveCanvasComment,
  onSelectedCollectionIdsChange,
  onSelectedDocumentIdChange,
  onResponseFormChange,
  onStop,
  onSubmit,
  onSelectedSkillIdsChange,
  onSourcePolicyChange,
  overview = null,
  reportOptions = [],
  reportCatalog = [],
  reportIds = [],
  reportIdsMax: reportLimit,
  onReportIdsChange,
  ruleOptions = [],
  selectedRuleIds = [],
  onSelectedRuleIdsChange,
  ruleIdsMax: ruleLimit,
  ruleComposedCharsMax = null,
  onOpenPromptLibrary,
  responseForm = 'auto',
  running = false,
  selectedCollectionIds,
  selectedDocumentId = null,
  selectedSkillIds = [],
  statusExecution = null,
  sourcePolicy,
  knowledgeSwitch,
  onKnowledgeSwitchChange,
  sourceAvailability = { web: true, knowledge: true },
  toolUseCounts = { web: 0, knowledge: 0 },
  maxAttachedSkills = 3,
  slashSkills = [],
}: {
  /** A clarification is pending: the send answers it (ONE input locus)
   * instead of starting a new run, and the placeholder says so. */
  answerMode?: boolean
  autonomy: string
  /** From `capabilities.agent.autonomy_modes` — never hardcoded. */
  autonomyModes: string[]
  /** Queued canvas selection comments — stacked rows above
   * the input; they ride the next submission's canvas_context. */
  canvasComments?: import('./canvas/commentQueue').AgentCanvasCommentDraft[]
  /** Pencil on a stacked row: jump to the anchor + edit. */
  onEditCanvasComment?: (id: string) => void
  /** Session canvas documents for the @-mention group; empty
   * hides the group. */
  canvasDocuments?: AgentCanvasDocumentOption[]
  /** The mention-pinned canvas document (caller-owned, like the comment
   * queue); rides the next submission when no comments are queued. */
  pinnedCanvasDocumentId?: string | null
  onPinnedCanvasDocumentChange?: (artifactId: string | null) => void
  collections: AgentCollectionOption[]
  /** Finished Research-Desk reports that can be attached as INPUT.
   * Empty hides the group entirely. */
  reportOptions?: import('@/features/project/selectors').CompletedReportOption[]
  /** Every finished report the desk knows: an attached report is named, and
   * found missing, against these — the menus offer only `reportOptions`, and
   * a report kept out of them still exists. */
  reportCatalog?: import('@/features/project/selectors').CompletedReportOption[]
  /** Attached report ids for the next submission. Ids only — the server
   * resolves the name and the kernel fetches the body on demand. */
  reportIds?: string[]
  /** Server cap on attached reports (published == enforced). Required,
   * with no default, like the rule cap: null = not published, so the count
   * is not pre-checked and the report category says so. */
  reportIdsMax: number | null
  onReportIdsChange?: (reportIds: string[]) => void
  /** Prompt-library rules this surface may attach, keyed by the id the
   * server resolves. Empty hides the category. */
  ruleOptions?: AgentRuleSource[]
  /** Attached rule ids for the next submission — inline pills, like
   * skills; caller-owned and cleared only by an accepted submission. */
  selectedRuleIds?: string[]
  onSelectedRuleIdsChange?: (ruleIds: string[]) => void
  /** Server cap on distinct rules (published == enforced). Required, with
   * no default: null = not published, so the count is not pre-checked and
   * the menu says so — never a second copy of the server's number. */
  ruleIdsMax: number | null
  /** The server's composed ceiling; null = not published, so the size is
   * not pre-checked and the menu says so. */
  ruleComposedCharsMax?: number | null
  /** Opens the prompt library — where an empty rule menu is resolved. */
  onOpenPromptLibrary?: () => void
  disabled?: boolean
  /** Patchable editor documents (server-synced or demo); empty hides the
   * document scope entirely. */
  documents?: AgentDocumentOption[]
  /** The draft's document with its capsules, as the desk holds it: the
   * input starts from it, so a view switch brings the capsules back where
   * the user put them. */
  draftDoc?: JSONContent | null
  draftQuestion: string
  /** Every edit of the document, for the desk to hold. */
  onDraftDocChange?: (doc: JSONContent) => void
  /** Thoroughness; caller-owned like `autonomy`. */
  depthMode?: 'normal' | 'deep'
  /** True only when the server publishes `agent.depth_modes` with
   * 'deep' — the toggle hides otherwise (feature detection). */
  depthSelectable?: boolean
  /** Effective Stufe; only meaningful when `tiers` is published. */
  tierMode?: AgentTierId | null
  /** Published Stufen ladder (`capabilities.agent.tiers`); non-empty
   * replaces the legacy depth toggle with the Stufe control. */
  tiers?: AgentTierCapability[] | null
  onTierModeChange?: (tier: AgentTierId | null) => void
  /** The selected engine; caller-owned like `autonomy`. */
  engineMode?: AgentEngineMode
  /** True only when the server registered the kernel
   * (`features.agent_kernel`) — the picker hides otherwise. */
  kernelSelectable?: boolean
  /** Account preference `enable_agent_memory`, shown in the run overview. */
  memoryEnabled?: boolean
  /** Model/effort override picker; null hides it (no catalog/health). */
  modelPicker?: AgentModelPickerProps | null
  /** Loud availability note (agent feature off / server missing). */
  notice?: string | null
  /** The server's refusal of the last submission (raw message); rendered
   * as an alert beside the draft, which stays for a retry. */
  submitError?: string | null
  onAutonomyChange: (autonomy: string) => void
  onDepthModeChange?: (mode: 'normal' | 'deep') => void
  onDraftQuestionChange: (draft: string) => void
  onEngineModeChange?: (mode: AgentEngineMode) => void
  onRemoveCanvasComment?: (id: string) => void
  onSelectedCollectionIdsChange: (ids: string[]) => void
  onSelectedDocumentIdChange?: (id: string | null) => void
  onResponseFormChange?: (form: AgentResponseForm) => void
  onSelectedSkillIdsChange?: (ids: string[]) => void
  onSourcePolicyChange: (policy: AgentSourcePolicy) => void
  onStop: () => void
  onSubmit: (submit: AgentComposerSubmit) => boolean | Promise<boolean>
  /** Server-fact summary for the run overview; null hides the menu
   * (older server without the agent capabilities block). */
  overview?: AgentOverview | null
  /** Output-form override; caller-owned like `autonomy`. */
  responseForm?: AgentResponseForm
  running?: boolean
  selectedCollectionIds: string[]
  selectedDocumentId?: string | null
  /** Attached skill ids (chips); caller-owned like `autonomy`. */
  selectedSkillIds?: string[]
  /** Accepted run facts for the read-only overview. The source dock still
   * reflects the next message's composer state. */
  statusExecution?: AgentExecutionSnapshot | null
  sourcePolicy: AgentSourcePolicy
  /** The session knowledge switch; absent until `/wissen` switched it on. */
  knowledgeSwitch?: AgentKnowledgeSwitch
  onKnowledgeSwitchChange?: (next: AgentKnowledgeSwitch) => void
  /** Deployment availability, separate from the session's choice. */
  sourceAvailability?: {
    web: boolean
    knowledge: boolean
    /** Server reason while knowledge is unavailable (`operator_disabled`,
     * `store_unreachable`). */
    knowledgeReason?: string | null
  }
  toolUseCounts?: AgentToolUseCounts
  /** `capabilities.agent.skills.max_attached` (soft gate; the server
   * enforces the hard one). */
  maxAttachedSkills?: number
  /** Slash-menu skills (autocomplete-enabled, caller-filtered). */
  slashSkills?: { id: string; label: string; description: string; argument_hint: string }[]
}) {
  const { locale, t } = useLocale()
  const engineRefusalId = useId()
  const inputRef = useRef<AgentInlineInputHandle | null>(null)
  const [question, setQuestion] = useState(draftQuestion)
  const [mention, setMention] = useState<CollectionMentionState | null>(null)
  const [mentionScope, setMentionScope] = useState<AgentContextCategoryId | null>(null)
  const [mentionIndex, setMentionIndex] = useState(0)
  const [slash, setSlash] = useState<SkillSlashState | null>(null)
  const [submitting, setSubmitting] = useState(false)

  // Echo guard: the parent mirrors every keystroke back through the
  // draftQuestion prop. Only an EXTERNAL change (view-switch restore,
  // programmatic clear) may replace the text and close the menus — the
  // echo of our own keystroke must not, or the @- and /-menus die
  // between events (visible as flicker, fatal for programmatic input).
  const questionRef = useRef(draftQuestion)
  useEffect(() => {
    if (draftQuestion === questionRef.current) return
    questionRef.current = draftQuestion
    setQuestion(draftQuestion)
    inputRef.current?.setText(draftQuestion)
    setMention(null)
    setSlash(null)
  }, [draftQuestion])
  useEffect(() => {
    questionRef.current = question
    onDraftQuestionChange(question)
  }, [question, onDraftQuestionChange])

  const selectedCollections = collections.filter((collection) =>
    selectedCollectionIds.includes(collection.id))
  const selectedDocument =
    documents.find((document) => document.id === selectedDocumentId) ?? null
  const pinnedCanvasDocument =
    canvasDocuments.find(
      (document) => document.artifactId === pinnedCanvasDocumentId,
    ) ?? null
  // Single-document channel: queued comments already bind the
  // attachment to THEIR document, so canvas documents stop being offered
  // while the queue is non-empty — the conflict cannot arise.
  const offeredCanvasDocuments = useMemo(
    () => (canvasComments.length > 0 ? [] : canvasDocuments),
    [canvasComments.length, canvasDocuments],
  )
  const reportTitle = (runId: string): string | null => (
    reportCatalog.find((report) => report.runId === runId)
    ?? reportOptions.find((report) => report.runId === runId)
  )?.title ?? null
  /** Capsules whose document is gone: marked on the capsule and named by
   * the notice above the input; the send waits until they are removed —
   * never a silently dropped id, never a raw id as their name. */
  const unavailablePills = [
    ...reportIds
      .filter((runId) => reportTitle(runId) === null)
      .map((runId) => commandPillKey('report', runId)),
    ...(pinnedCanvasDocumentId !== null && pinnedCanvasDocument === null
      ? [commandPillKey('canvas', pinnedCanvasDocumentId)]
      : []),
    ...(selectedDocumentId !== null && selectedDocument === null
      ? [commandPillKey('editTarget', selectedDocumentId)]
      : []),
  ]
  // ONE model behind "+" and "@": the same categories, counts and items.
  const contextCategories = useMemo(
    () => agentContextCategories({
      answerMode,
      collections,
      selectedCollectionIds,
      documents,
      selectedDocumentId,
      canvasDocuments: offeredCanvasDocuments,
      pinnedCanvasDocumentId,
      engine: engineMode,
      reports: reportOptions,
      reportIds,
      reportLimit,
      reportsAttachable: Boolean(onReportIdsChange),
      ruleCharLimit: ruleComposedCharsMax,
      ruleIds: selectedRuleIds,
      ruleLimit,
      rules: ruleOptions,
      rulesAttachable: Boolean(onSelectedRuleIdsChange),
    }),
    [
      answerMode,
      collections,
      documents,
      engineMode,
      onReportIdsChange,
      onSelectedRuleIdsChange,
      offeredCanvasDocuments,
      pinnedCanvasDocumentId,
      reportIds,
      reportLimit,
      reportOptions,
      ruleComposedCharsMax,
      ruleLimit,
      ruleOptions,
      selectedCollectionIds,
      selectedDocumentId,
      selectedRuleIds,
    ],
  )
  const categoryLabel = (id: AgentContextCategoryId): string =>
    id === 'rule'
      ? t.agent.composer.ruleGroup
      : id === 'collection'
        ? t.knowledge.collectionPickerTitle
        : id === 'document'
          ? t.agent.composer.editTarget
          : id === 'canvas'
            ? t.agent.composer.canvasDocGroup
            : t.agent.composer.reportGroup
  // The chat's glyphs: BookOpen is a rule, FileText a document or report.
  const categoryIcon = (id: AgentContextCategoryId) =>
    id === 'collection' ? Database : id === 'rule' ? BookOpen : FileText
  const categoryTone = (id: AgentContextCategoryId): MentionTone =>
    id === 'document' ? 'file' : id === 'rule' ? 'success' : 'brand'
  // The words for a category and its entries: the same on the "@" root
  // row, in the scoped header, in "+" and on every blocked row.
  const { categoryBlockText, categoryNote, itemSecondary, limitsLabel } = agentContextText(
    t,
    locale,
    { report: reportLimit, rule: ruleLimit },
  )
  // `@rules:` and `@research:` read as a scope HERE, in this composer's
  // own resolution — the shared detector stays a plain query for the
  // knowledge composer and the plan-gate editor.
  const mentionTarget = useMemo(
    () => (mention ? agentMentionTarget(mention.query, mentionScope) : null),
    [mention, mentionScope],
  )
  const mentionRows = useMemo(
    () => (mentionTarget
      ? agentMentionRows(contextCategories, mentionTarget.scope, mentionTarget.filter)
      : []),
    [contextCategories, mentionTarget],
  )
  /** Why the menu has nothing to list, while it has nothing to list. */
  const mentionEmpty = mentionTarget && mentionRows.length === 0
    ? agentMentionEmptyState({
      categories: contextCategories,
      engine: engineMode,
      filter: mentionTarget.filter,
      scope: mentionTarget.scope,
    })
    : null
  /** What picking a row does (`undefined` = the state row shown while
   * there is nothing to list) — the ONE answer for the click, the Enter
   * key and whether the row is marked as not pickable. */
  const mentionPick = (row: AgentMentionRow | undefined) =>
    agentMentionPick(row, mentionEmpty, { libraryAvailable: Boolean(onOpenPromptLibrary) })
  const mentionOptions: MentionMenuOption[] = mentionRows.map((row) =>
    row.kind === 'category'
      ? {
        group: undefined,
        icon: categoryIcon(row.category.id),
        isCategory: true,
        primary: categoryLabel(row.category.id),
        secondary: categoryNote(row.category),
        tone: categoryTone(row.category.id),
      }
      : {
        disabled: mentionPick(row).kind === 'none',
        group: mentionTarget?.scope ? undefined : categoryLabel(row.category),
        icon: categoryIcon(row.category),
        isCategory: false,
        primary: row.item.label,
        secondary: itemSecondary(row.category, row.item),
        tone: categoryTone(row.category),
      })
  function openPromptLibrary() {
    setMention(null)
    setMentionScope(null)
    onOpenPromptLibrary?.()
  }
  /** The one row the menu shows when it has nothing to list — a sentence
   * about THIS situation, never a missing collection for every case. */
  const mentionEmptyOption = (empty: AgentMentionEmptyState): MentionMenuOption => {
    // A state, not an entry: read in full, and marked as not pickable
    // unless it has an action (opening the prompt library).
    const base = {
      disabled: mentionPick(undefined).kind === 'none',
      group: undefined,
      isCategory: false,
      state: true,
    }
    if (empty.kind === 'noMatch') {
      return {
        ...base,
        icon: empty.scope ? categoryIcon(empty.scope) : Search,
        primary: t.agent.composer.mentionNoMatch.replace('{query}', () => empty.query),
        secondary: empty.scope
          ? t.agent.composer.mentionNoMatchIn.replace('{category}', categoryLabel(empty.scope))
          : t.agent.composer.mentionNoMatchAll,
        tone: empty.scope ? categoryTone(empty.scope) : 'brand',
      }
    }
    if (empty.kind === 'blocked') {
      const category = contextCategories.find((entry) => entry.id === empty.scope)
      return {
        ...base,
        icon: categoryIcon(empty.scope),
        primary: (category && categoryBlockText(category)) ?? categoryLabel(empty.scope),
        secondary: empty.reason === 'allAttached'
          ? t.agent.composer.ruleAllAttachedHint
          : categoryLabel(empty.scope),
        tone: categoryTone(empty.scope),
      }
    }
    if (empty.kind === 'noRules') {
      return {
        ...base,
        icon: BookOpen,
        primary: t.agent.composer.mentionNoRulesTitle,
        secondary: t.agent.composer.mentionNoRulesHint,
        tone: 'success',
      }
    }
    if (empty.kind === 'kernelOnly') {
      return {
        ...base,
        icon: categoryIcon(empty.scope),
        primary: t.agent.composer.mentionKernelOnly.replace(
          '{engine}',
          engineModeLabel('agent_kernel', t),
        ),
        secondary: categoryLabel(empty.scope),
        tone: categoryTone(empty.scope),
      }
    }
    if (empty.kind === 'emptyCategory') {
      return {
        ...base,
        icon: categoryIcon(empty.scope),
        primary: t.agent.composer.mentionEmptyCategory,
        secondary: categoryLabel(empty.scope),
        tone: categoryTone(empty.scope),
      }
    }
    return {
      ...base,
      icon: Plus,
      primary: t.agent.composer.mentionNothingTitle,
      secondary: t.agent.composer.mentionNothingHint,
      tone: 'brand',
    }
  }
  const attachContextItem = (category: AgentContextCategoryId, id: string) => {
    if (category === 'rule') {
      // From "+": no typed token to replace, so the pill joins as a
      // setting (see `commandPills`) and its label stays out of the text.
      onSelectedRuleIdsChange?.(ruleIdsForSubmit([...selectedRuleIds, id]))
    } else if (category === 'collection') {
      onSelectedCollectionIdsChange([...selectedCollectionIds, id])
    } else if (category === 'document') {
      onSelectedDocumentIdChange?.(id)
    } else if (category === 'report') {
      onReportIdsChange?.([...reportIds, id])
    } else {
      onPinnedCanvasDocumentChange?.(id)
    }
  }

  const slashCandidates = useMemo(() => {
    if (!slash) return []
    const canAttachMore = selectedSkillIds.length < maxAttachedSkills
    const skillMatches = canAttachMore
      ? slashSkills
        .filter(
          (skill) =>
            !selectedSkillIds.includes(skill.id)
            && skill.label.includes(slash.query),
        )
        .map((skill) => ({ kind: 'skill' as const, skill }))
      : []
    // A source command is offered only while it would change something:
    // `/web` while the web is off, `/wissen` while the session's knowledge
    // switch is not on yet and no chip pins the scope already. Depth is the
    // tier's job — no second web control here.
    const commandMatches = COMPOSER_COMMAND_OPTIONS.filter((command) => {
      if (!command.token.includes(slash.query)) return false
      if (command.id === 'web') {
        return sourceAvailability.web && sourcePolicy.web !== 'available'
      }
      // `/canvas` is offered while it would change something, exactly like
      // the two source commands: the form is not pinned to canvas yet.
      if (command.id === 'canvas') {
        return Boolean(onResponseFormChange) && responseForm !== 'canvas'
      }
      return (
        sourceAvailability.knowledge
        && knowledgeSwitch !== 'all'
        && selectedCollectionIds.length === 0
      )
    }).map((command) => ({ kind: 'command' as const, command }))
    return [...commandMatches, ...skillMatches]
  }, [
    knowledgeSwitch,
    maxAttachedSkills,
    onResponseFormChange,
    responseForm,
    selectedCollectionIds.length,
    selectedSkillIds,
    slash,
    slashSkills,
    sourceAvailability.knowledge,
    sourceAvailability.web,
    sourcePolicy.web,
  ])
  const slashOptions: MentionMenuOption[] = slashCandidates.map((candidate) =>
    candidate.kind === 'skill'
      ? {
        group: t.skills.title,
        icon: Sparkles,
        isCategory: false,
        primary: `/${candidate.skill.label}`,
        secondary:
          candidate.skill.argument_hint || candidate.skill.description,
        tone: 'success',
      }
      : {
        group: t.agent.composer.slashCommands,
        icon: commandIcon(candidate.command.id),
        isCategory: false,
        primary: commandLabel(candidate.command.id, t),
        secondary: commandHint(candidate.command.id, t),
        tone: 'brand',
      })

  // answerMode overrides the running gate: a parked run WAITS for this
  // input — the send answers the clarification instead of racing a run.
  // Attached comments make the submission self-sufficient — the
  // backend instructs the model to address every comment, so an empty
  // input may send. The visible default question below keeps the
  // transcript honest (nothing travels silently).
  // Reports and canvas context feed the kernel only: under the mission
  // engine the server would refuse the send, so the capsules say it and the
  // send waits, like a document that is gone.
  const engineRefusal = engineRefusesAttachments({
    canvasAttached: canvasComments.length > 0 || pinnedCanvasDocument !== null,
    engine: engineMode,
    reportCount: reportIds.length,
  })
  const engineRefusalText = t.agent.composer.kernelOnlyAttached.replace(
    '{engine}',
    engineModeLabel('agent_kernel', t),
  )
  const unavailableKey = unavailablePills.join('|')
  /** What a capsule cannot know itself: whether the engine refuses its kind
   * (warning tone, described by the notice above the input), and whether its
   * document is gone. */
  const pillState = useMemo<CommandPillState>(() => ({
    refusalNoticeId: engineRefusalId,
    refusedKinds: engineRefusal ? ['report', 'canvas'] : [],
    unavailable: unavailableKey ? unavailableKey.split('|') : [],
  }), [engineRefusal, engineRefusalId, unavailableKey])
  const canSubmit =
    !disabled
    && !submitting
    && (answerMode || !running)
    && unavailablePills.length === 0
    && !engineRefusal
    && (question.trim().length > 0 || canvasComments.length > 0)
  const placeholder = answerMode
    ? t.agent.timeline.answerPlaceholder
    : canvasComments.length > 0
      ? t.agent.composer.canvasCommentsPlaceholder
      : t.agent.composer.placeholder

  // Both detectors are caret-anchored (`start = caret - query.length - 1`),
  // so the text BEFORE the caret is all they need — and it is the only thing
  // a document with inline pills can offer honestly: a pill occupies a
  // document position but no characters, so absolute string indices into the
  // whole value would be wrong the moment one exists.
  function updateMenusFromCaret() {
    const before = inputRef.current?.textBeforeCaret() ?? ''
    const nextMention = detectCollectionMention(before, before.length)
    setMention(nextMention)
    if (!nextMention) setMentionScope(null)
    setSlash(nextMention ? null : detectSkillSlash(before, before.length))
    setMentionIndex(0)
  }

  function handleQuestionChange(text: string) {
    questionRef.current = text
    setQuestion(text)
  }

  function selectMentionOption(index: number) {
    const pick = mentionPick(mentionRows[index])
    if (pick.kind === 'openLibrary') {
      openPromptLibrary()
      return
    }
    if (pick.kind === 'none' || !mention) return
    if (pick.kind === 'enter') {
      // One step in: the "@" stays, the typed filter resets. A blocked
      // category opens too — inside, its entries or its state say why.
      inputRef.current?.deleteBeforeCaret(mention.query.length)
      setMention({ query: '', start: mention.start })
      setMentionScope(pick.scope)
      setMentionIndex(0)
      return
    }
    // `@` plus everything typed after it (an alias included) — removed
    // caret-relative, never by index.
    const tokenLength = mention.query.length + 1
    const kind = CAPSULE_KIND[pick.category]
    if (kind) {
      // A rule or a document sits in the sentence where the user picked
      // it, like a skill: the pill replaces the token IN PLACE, and the
      // attached ids follow from the document (`handlePillsEdited`) — one
      // path.
      inputRef.current?.replaceTokenWithPill(tokenLength, {
        id: pick.item.id,
        kind,
        label: pick.item.label,
      })
    } else {
      inputRef.current?.deleteBeforeCaret(tokenLength)
      attachContextItem(pick.category, pick.item.id)
    }
    setMention(null)
    setMentionScope(null)
  }

  /** Leave the scope: a stepped-into category resets, an alias the user
   * typed is deleted, so the "@" opens on the categories again. */
  function leaveMentionScope() {
    if (mention && mentionTarget?.alias) {
      inputRef.current?.deleteBeforeCaret(mention.query.length)
      setMention({ query: '', start: mention.start })
    }
    setMentionScope(null)
    setMentionIndex(0)
  }

  function selectSlashOption(index: number) {
    const candidate = slashCandidates[index]
    if (!candidate || !slash) return
    // The typed token is `/` plus the filter — removed caret-relative.
    const tokenLength = slash.query.length + 1
    if (candidate.kind === 'skill') {
      // The pill replaces the token IN PLACE; the switch state follows from
      // the document (see `onPillsEdited`), so there is exactly one path.
      inputRef.current?.replaceTokenWithPill(tokenLength, {
        id: candidate.skill.id,
        kind: 'skill',
        label: candidate.skill.label,
      })
    } else {
      const effect = composerCommandEffect(candidate.command.id)
      if (effect.knowledgeSwitch) {
        inputRef.current?.replaceTokenWithPill(tokenLength, {
          id: '',
          kind: 'knowledge',
          label: t.agent.composer.knowledgeChip,
        })
      } else if (effect.responseForm) {
        inputRef.current?.replaceTokenWithPill(tokenLength, {
          id: effect.responseForm,
          kind: 'responseForm',
          label: responseFormLabel(effect.responseForm, t),
        })
      } else {
        // `/web` carries no pill: the web search is on by default, so a
        // permanent capsule for it would be noise.
        inputRef.current?.deleteBeforeCaret(tokenLength)
        if (effect.web) onSourcePolicyChange({ ...sourcePolicy, web: effect.web })
      }
    }
    setSlash(null)
  }

  /** The pills the document must carry — derived from the switches, so the
   * run-setup menu, a restored draft and the `/`-command all land in the same
   * place without a second piece of state. */
  const commandPills: CommandPillSpec[] = [
    ...(knowledgeSwitch === 'all'
      ? [{ id: '', kind: 'knowledge' as const, label: t.agent.composer.knowledgeChip }]
      : []),
    ...(responseForm !== 'auto'
      ? [{
        id: responseForm,
        kind: 'responseForm' as const,
        label: responseFormLabel(responseForm, t),
      }]
      : []),
    ...selectedSkillIds.map((skillId) => ({
      id: skillId,
      kind: 'skill' as const,
      label: slashSkills.find((skill) => skill.id === skillId)?.label ?? skillId,
    })),
    ...ruleIdsForSubmit(selectedRuleIds).map((ruleId) => ({
      id: ruleId,
      kind: 'rule' as const,
      label: ruleOptions.find((rule) => rule.id === ruleId)?.title ?? ruleId,
    })),
    // A document whose name is gone is labelled as gone, never by its id.
    ...reportIds.map((runId) => ({
      id: runId,
      kind: 'report' as const,
      label: reportTitle(runId) ?? t.agent.composer.pillUnavailable,
    })),
    ...(pinnedCanvasDocumentId !== null
      ? [{
        id: pinnedCanvasDocumentId,
        kind: 'canvas' as const,
        label: pinnedCanvasDocument?.name ?? t.agent.composer.pillUnavailable,
      }]
      : []),
    ...(selectedDocumentId !== null
      ? [{
        id: selectedDocumentId,
        kind: 'editTarget' as const,
        label: selectedDocument?.title ?? t.agent.composer.pillUnavailable,
      }]
      : []),
  ]

  /** The user edited the document: what is LEFT in it is the truth. This is
   * the only place a removal turns into "switch off" — deleting a pill is
   * deleting a word, and the switch follows. */
  function handlePillsEdited(pills: CommandPillSpec[]) {
    const hasKnowledge = pills.some((pill) => pill.kind === 'knowledge')
    if (hasKnowledge !== (knowledgeSwitch === 'all')) {
      onKnowledgeSwitchChange?.(hasKnowledge ? 'all' : 'off')
    }
    const form = pills.find((pill) => pill.kind === 'responseForm')
    const nextForm = (form?.id ?? 'auto') as AgentResponseForm
    if (nextForm !== responseForm) onResponseFormChange?.(nextForm)
    const skillIds = pills
      .filter((pill) => pill.kind === 'skill')
      .map((pill) => pill.id)
    if (skillIds.join('|') !== selectedSkillIds.join('|')) {
      onSelectedSkillIdsChange?.(skillIds)
    }
    // A pasted copy of a pill is still one rule.
    const ruleIds = ruleIdsForSubmit(
      pills.filter((pill) => pill.kind === 'rule').map((pill) => pill.id),
    )
    if (ruleIds.join('|') !== selectedRuleIds.join('|')) {
      onSelectedRuleIdsChange?.(ruleIds)
    }
    // A pasted copy of a document is still one attachment; the canvas
    // document and the editing target hold one slot each.
    const attachedReports = [...new Set(
      pills.filter((pill) => pill.kind === 'report').map((pill) => pill.id),
    )]
    if (attachedReports.join('|') !== reportIds.join('|')) {
      onReportIdsChange?.(attachedReports)
    }
    const canvasPin = pills.find((pill) => pill.kind === 'canvas')?.id ?? null
    if (canvasPin !== pinnedCanvasDocumentId) onPinnedCanvasDocumentChange?.(canvasPin)
    const editTarget = pills.find((pill) => pill.kind === 'editTarget')?.id ?? null
    if (editTarget !== selectedDocumentId) onSelectedDocumentIdChange?.(editTarget)
  }

  /** Runs inside the editor's `handleKeyDown`, so it takes a NATIVE event and
   * returns whether the key was consumed — TipTap must not also apply it. */
  function handleKeyDown(event: KeyboardEvent): boolean {
    if (slash && slashCandidates.length > 0) {
      if (event.key === 'ArrowDown') {
        setMentionIndex((current) => (current + 1) % slashCandidates.length)
        return true
      }
      if (event.key === 'ArrowUp') {
        setMentionIndex(
          (current) =>
            (current - 1 + slashCandidates.length) % slashCandidates.length,
        )
        return true
      }
      // Enter always takes the highlighted entry — also when the user
      // typed the full token: the menu is open, its hint says "↵ select".
      if (event.key === 'Tab' || event.key === 'Enter') {
        selectSlashOption(mentionIndex)
        return true
      }
    }
    if (slash && event.key === 'Escape') {
      setSlash(null)
      return true
    }
    if (mention && mentionScope && event.key === 'Backspace' && mention.query === '') {
      leaveMentionScope()
      return true
    }
    if (mention && mentionOptions.length > 0) {
      if (event.key === 'ArrowDown') {
        setMentionIndex((current) => (current + 1) % mentionOptions.length)
        return true
      }
      if (event.key === 'ArrowUp') {
        setMentionIndex(
          (current) =>
            (current - 1 + mentionOptions.length) % mentionOptions.length,
        )
        return true
      }
      if (event.key === 'Enter' || event.key === 'Tab') {
        selectMentionOption(mentionIndex)
        return true
      }
    }
    if (
      mention
      && mentionPick(undefined).kind === 'openLibrary'
      && (event.key === 'Enter' || event.key === 'Tab')
    ) {
      openPromptLibrary()
      return true
    }
    if (mention && (event.key === 'Enter' || event.key === 'Tab')) {
      // The menu shows a state row with nothing to pick: Enter closes it
      // and sends nothing — a half-typed "@…" token is no task.
      setMention(null)
      setMentionScope(null)
      return true
    }
    if (mention && event.key === 'Escape') {
      setMention(null)
      setMentionScope(null)
      return true
    }
    if (
      event.key === 'Enter'
      && !event.ctrlKey
      && !event.metaKey
      && !event.shiftKey
      && !event.isComposing
    ) {
      void submit()
      return true
    }
    return false
  }

  async function submit() {
    if (!canSubmit) return
    // What the user WROTE: a capsule they typed as a word is spelled out (it
    // holds a document position but no character, so the bare text would send
    // their sentence with a hole in it), a capsule a switch placed is a setting
    // and never becomes text. Either way the effect travels in the request
    // fields below. An answer to a clarification holds the rule and document
    // pills back: not spelled into the answer, and kept in the draft for the
    // next task. A document is named in the reader's quotes.
    const keptByAnswer = pillKindsKeptByAnswer(answerMode)
    const quote = (label: string) => t.agent.composer.documentQuote.replace('{label}', () => label)
    const written = inputRef.current?.textWithPillLabels(keptByAnswer, quote) ?? question
    const trailing = extractTrailingComposerCommand(written)
    // A trailing `/web` switches the session's web search on (kept on
    // afterwards, like the icon); `/wissen` switches the session's
    // knowledge on the same way — every visible collection, until the
    // knowledge glyph switches it off again. Chips keep pinning.
    // The token applies whatever the manifest said about availability: a
    // source the server currently refuses is refused visibly at submit
    // (503 with the reason), never dropped from the request on the client.
    const effect = trailing ? composerCommandEffect(trailing.command) : {}
    const submitWeb = effect.web ?? sourcePolicy.web
    const submitSwitch = effect.knowledgeSwitch ?? knowledgeSwitch
    const submitResponseForm = effect.responseForm ?? responseForm
    const knowledgeScope = knowledgeScopeForSubmit({
      collectionIds: selectedCollectionIds,
      knowledgeSwitch: submitSwitch,
    })
    // With attached comments an empty input sends the VISIBLE
    // default question (it lands in the bubble like any typed text).
    const submittedQuestion = (trailing?.question ?? written.trim())
      || (canvasComments.length > 0
        ? t.agent.composer.canvasCommentsDefaultQuestion
        : '')
    if (!submittedQuestion) return
    setSubmitting(true)
    try {
      if (submitWeb !== sourcePolicy.web) {
        onSourcePolicyChange({ ...sourcePolicy, web: submitWeb })
      }
      if (submitSwitch !== knowledgeSwitch && submitSwitch !== undefined) {
        onKnowledgeSwitchChange?.(submitSwitch)
      }
      if (submitResponseForm !== responseForm) {
        onResponseFormChange?.(submitResponseForm)
      }
      const accepted = await onSubmit({
        autonomy,
        collectionIds: selectedCollectionIds,
        knowledgeSwitch: submitSwitch,
        depth: depthMode,
        tier: tierMode,
        // Resolved against the current options — a selection whose document
        // vanished (deleted / sync toggled off) must not submit a stale id.
        documentId: selectedDocument?.id,
        engineMode,
        question: submittedQuestion,
        responseForm: submitResponseForm,
        skillIds: selectedSkillIds,
        knowledgeScope,
        sourcePolicy: {
          web: submitWeb,
          knowledge: knowledgePolicyForScope(knowledgeScope),
        },
      })
      if (!accepted) return
      setQuestion('')
      inputRef.current?.clear(pillKindsKeptAfterSend(answerMode))
      setMention(null)
      setSlash(null)
    } finally {
      setSubmitting(false)
    }
  }

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    void submit()
  }

  return (
    <form className="agent-composer-container mx-auto max-w-5xl" onSubmit={handleSubmit}>
      {/* Stacked pending canvas comments: an own card ABOVE
          the composer frame — the editor attach panel's design language
          (frame, header, entrance animation) with the agent's rows
          (quote + text + pencil + trash: each pending comment stays
          editable and removable until it is sent). */}
      <AnimatePresence initial={false}>
        {canvasComments.length > 0 ? (
          <motion.div
            animate={{ height: 'auto', opacity: 1 }}
            className="mb-2 overflow-hidden rounded-md border border-brand/30 bg-brand-subtle/30"
            exit={{ height: 0, opacity: 0 }}
            initial={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.18 }}
          >
            <div className="flex items-center gap-1.5 px-3 pb-0.5 pt-2 t-meta-sm font-semibold text-brand">
              <MessageSquareText aria-hidden="true" className="size-3.5" />
              {countLabel(
                canvasComments.length,
                t.agent.composer.canvasCommentHeaderOne,
                t.agent.composer.canvasCommentHeader,
              )}
            </div>
            <div className="flex flex-col px-2 pb-2">
              {canvasComments.map((comment, index) => (
                <div
                  className="flex min-w-0 items-center gap-1.5 rounded px-1 py-0.5"
                  key={comment.id}
                >
                  <span className="grid size-4 shrink-0 place-items-center rounded-[4px] bg-brand-subtle t-hint font-semibold tabular-nums text-brand">
                    {index + 1}
                  </span>
                  <span
                    className="max-w-44 shrink-0 truncate t-meta-sm text-muted-foreground"
                    title={comment.plainText || comment.quote}
                  >
                    „{comment.plainText || comment.quote}“
                  </span>
                  {/* Full text stays reachable at the cut. */}
                  <span
                    className="min-w-0 flex-1 truncate t-meta-sm text-foreground"
                    title={comment.comment}
                  >
                    {comment.comment}
                  </span>
                  <Button
                    aria-label={t.agent.composer.editCanvasComment}
                    className="size-5 shrink-0 text-muted-foreground hover:text-foreground"
                    onClick={() => onEditCanvasComment?.(comment.id)}
                    size="icon"
                    type="button"
                    variant="ghost"
                  >
                    <PenLine className="size-3" />
                  </Button>
                  <Button
                    aria-label={t.agent.composer.removeCanvasComment}
                    className="size-5 shrink-0 text-muted-foreground hover:text-destructive"
                    onClick={() => onRemoveCanvasComment?.(comment.id)}
                    size="icon"
                    type="button"
                    variant="ghost"
                  >
                    <Trash2 className="size-3" />
                  </Button>
                </div>
              ))}
            </div>
          </motion.div>
        ) : null}
      </AnimatePresence>
        <div className="relative overflow-visible rounded-xl border border-border bg-card px-2.5 py-2 shadow-[0_8px_28px_-12px_var(--shadow-soft)] transition-[border-color,box-shadow] duration-150 focus-within:border-brand/60 focus-within:ring-2 focus-within:ring-brand/15">
        {mention && (
          <MentionMenu
            activeIndex={mentionIndex}
            labels={{
              backHint: t.chat.mentionBackHint,
              closeHint: t.chat.mentionCloseHint,
              filterPlaceholder: t.chat.mentionFilterPlaceholder,
              navHint: t.chat.mentionNavHint,
              rootTitle: t.agent.composer.addContext,
              selectHint: t.chat.mentionSelectHint,
            }}
            onBack={leaveMentionScope}
            onHover={setMentionIndex}
            onSelect={selectMentionOption}
            options={mentionEmpty ? [mentionEmptyOption(mentionEmpty)] : mentionOptions}
            scope={mentionTarget?.scope
              ? {
                icon: categoryIcon(mentionTarget.scope),
                kind: categoryLabel(mentionTarget.scope),
                query: mentionTarget.filter,
                tone: categoryTone(mentionTarget.scope),
                value: (() => {
                  const scoped = contextCategories.find(
                    (category) => category.id === mentionTarget.scope,
                  )
                  return scoped ? limitsLabel(scoped) : undefined
                })(),
              }
              : { kind: null, query: mention.query }}
          />
        )}

        {slash && (
          <MentionMenu
            activeIndex={mentionIndex}
            labels={{
              backHint: t.chat.mentionBackHint,
              closeHint: t.chat.mentionCloseHint,
              filterPlaceholder: t.chat.mentionFilterPlaceholder,
              navHint: t.chat.mentionNavHint,
              rootTitle: t.agent.composer.slashMenuTitle,
              selectHint: t.chat.mentionSelectHint,
            }}
            onHover={setMentionIndex}
            onSelect={selectSlashOption}
            options={slashOptions.length > 0
              ? slashOptions
              : [{
                // A state without an action: picking it selects nothing.
                disabled: true,
                group: undefined,
                icon: Sparkles,
                isCategory: false,
                state: true,
                primary: selectedSkillIds.length >= maxAttachedSkills
                  ? t.agent.composer.slashCapReached.replace(
                    '{count}',
                    String(maxAttachedSkills),
                  )
                  : t.agent.composer.slashNoMatches,
                secondary: t.agent.composer.slashHint,
                tone: 'success',
              }]}
            scope={{
              icon: Sparkles,
              kind: t.agent.composer.slashMenuTitle,
              query: slash.query,
              tone: 'success',
            }}
          />
        )}

        {notice && (
          <div className="mb-1.5 rounded-md border border-warning/25 bg-warning-subtle px-2 py-1 t-meta-sm font-semibold text-warning">
            {notice}
          </div>
        )}
        {submitError && (
          <div
            className="mb-1.5 rounded-md border border-destructive/30 bg-destructive/5 px-2 py-1 t-meta-sm font-semibold text-destructive"
            data-testid="agent-submit-error"
            role="alert"
          >
            {t.agent.composer.submitRefused.replace('{message}', submitError)}
          </div>
        )}
        {engineRefusal && (
          <div
            className="mb-1.5 rounded-md border border-warning/25 bg-warning-subtle px-2 py-1 t-meta-sm font-semibold text-warning"
            data-testid="agent-engine-refusal"
            id={engineRefusalId}
          >
            {engineRefusalText}
          </div>
        )}
        {unavailablePills.length > 0 && (
          <div
            className="mb-1.5 rounded-md border border-warning/25 bg-warning-subtle px-2 py-1 t-meta-sm font-semibold text-warning"
            data-testid="agent-unavailable-attachment"
            role="status"
          >
            {t.agent.composer.unavailableAttached}
          </div>
        )}

        {selectedCollections.length > 0 && (
          <div className="mb-1.5 flex flex-wrap items-center gap-1.5">
            {/* Collections scope the knowledge search: chips, not words.
                Rules, documents and skills are capsules in the sentence
                the user wrote, where "@" or "/" placed them. */}
            {selectedCollections.map((collection) => (
              <Chip
                active
                aria-label={`${t.knowledge.removeCollection}: ${collection.title}`}
                dot="bg-brand"
                key={collection.id}
                onClick={() =>
                  onSelectedCollectionIdsChange(
                    selectedCollectionIds.filter((id) => id !== collection.id),
                  )}
                title={t.knowledge.removeCollection}
              >
                <span className="max-w-48 truncate">{collection.title}</span>
                <X aria-hidden="true" className="size-3 shrink-0" />
              </Chip>
            ))}
          </div>
        )}

        <CommandPillStateContext.Provider value={pillState}>
          <AgentInlineInput
            ariaLabel={placeholder}
            disabled={disabled}
            initialDoc={draftDoc}
            onBlur={() => {
              setMention(null)
              setSlash(null)
            }}
            onCaretChange={updateMenusFromCaret}
            onDocChange={onDraftDocChange}
            onKeyDown={handleKeyDown}
            onPillsEdited={handlePillsEdited}
            onTextChange={handleQuestionChange}
            pills={commandPills}
            placeholder={placeholder}
            ref={inputRef}
          />
        </CommandPillStateContext.Provider>

        <div className="agent-composer-footer mt-1.5 flex items-center justify-between gap-2 border-t border-border/70 pt-1.5">
          <div className="agent-composer-primary flex min-w-0 flex-1 items-center gap-1.5">
            <DropdownMenu modal={false}>
              <DropdownMenuTrigger asChild>
                <Button
                  aria-label={t.agent.composer.addContext}
                  className={composerIconButtonClassName}
                  disabled={disabled || (contextCategories.length === 0 && !onOpenPromptLibrary)}
                  type="button"
                  variant="ghost"
                >
                  <Plus />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent
                align="start"
                className={cn(optionMenuContentClassName, 'w-72')}
                side="top"
                sideOffset={8}
              >
                <OptionMenuHeader
                  count={contextCategories.reduce(
                    (sum, category) => sum + pickableCount(category),
                    0,
                  )}
                  title={t.agent.composer.addContext}
                />
                <div className="py-1">
                  {/* The model already lists the categories in display
                      order, the same order as the "@" root. */}
                  {contextCategories.map((category) => {
                    const id = category.id
                    const Icon = categoryIcon(id)
                    return (
                      <DropdownMenuSub key={id}>
                        <DropdownMenuSubTrigger className={optionSubTriggerClassName}>
                          <Icon aria-hidden="true" className="icon-sm text-muted-foreground" />
                          <span className="min-w-0 flex-1 truncate">{categoryLabel(id)}</span>
                          <span className="t-hint tabular-nums text-muted-foreground/70">
                            {pickableCount(category)}
                          </span>
                        </DropdownMenuSubTrigger>
                        <DropdownMenuSubContent
                          className={cn(optionMenuContentClassName, 'w-80')}
                          sideOffset={6}
                        >
                          <OptionMenuHeader
                            count={pickableCount(category)}
                            title={categoryLabel(id)}
                            value={limitsLabel(category)}
                          />
                          {categoryBlockText(category) !== null ? (
                            <p className="px-2.5 py-2 t-meta text-muted-foreground">
                              {categoryBlockText(category)}
                            </p>
                          ) : (
                            <div className="max-h-72 overflow-y-auto py-1">
                              {category.items.map((item) => (
                                <OptionMenuItem
                                  active={false}
                                  description={itemSecondary(id, item)}
                                  disabled={Boolean(item.blocked)}
                                  icon={Icon}
                                  key={item.id}
                                  label={item.label}
                                  onSelect={() => attachContextItem(id, item.id)}
                                />
                              ))}
                            </div>
                          )}
                        </DropdownMenuSubContent>
                      </DropdownMenuSub>
                    )
                  })}
                </div>
                {/* The way to the library, as in the chat: where a rule is
                    created or enabled for the agent. */}
                {onOpenPromptLibrary && (
                  <>
                    <DropdownMenuSeparator className="mx-0 my-0" />
                    <OptionMenuItem
                      active={false}
                      description={t.chat.manageRulesDescription}
                      icon={Library}
                      label={t.chat.manageRules}
                      onSelect={openPromptLibrary}
                    />
                  </>
                )}
              </DropdownMenuContent>
            </DropdownMenu>

            <div
              aria-label={t.agent.composer.sources}
              className="agent-source-dock flex h-7 shrink-0 items-center gap-1"
              role="group"
            >
              {(() => {
                // ONE web switch with two visible states: on = brand chip,
                // off = struck-through glyph. `/web` sets the same state.
                const state = webSwitchState({
                  available: sourceAvailability.web,
                  policy: sourcePolicy.web,
                })
                const isOn = state === 'on'
                const stateHint = state === 'unavailable'
                  ? t.agent.composer.sourceUnavailable
                  : isOn
                    ? t.agent.composer.sourceOn
                    : t.agent.composer.sourceOff
                const revealLabel = (
                  state === 'unavailable'
                    ? t.agent.composer.sourceUnavailableShort
                    : isOn
                      ? t.agent.composer.sourceOnShort
                      : t.agent.composer.sourceOffShort
                ).replace('{source}', t.agent.composer.sourceWebLong)
                return (
                  <SourceToggle
                    hint={stateHint}
                    icon={Globe2}
                    label={t.agent.composer.sourceWeb}
                    locked={disabled}
                    // The next value follows the PERSISTED switch, not the
                    // visual state: an unavailable source that is still
                    // switched on must switch OFF on click.
                    onToggle={() =>
                      onSourcePolicyChange({
                        ...sourcePolicy,
                        web: sourcePolicy.web === 'available' ? 'disabled' : 'available',
                      })}
                    revealLabel={revealLabel}
                    source="web"
                    state={state}
                    switchedOn={sourcePolicy.web === 'available'}
                  />
                )
              })()}
              {(() => {
                // The knowledge switch shows only once the session asked for
                // project knowledge (`/wissen`), and never beside a chip — a
                // chip pins the scope itself. On = every visible collection,
                // off = struck-through glyph; a click toggles, `/wissen`
                // switches on. While the server reports the store
                // unavailable the glyph greys out with the reason and can
                // only be switched off.
                const state = knowledgeSwitchState({
                  available: sourceAvailability.knowledge,
                  knowledgeSwitch,
                  pinned: selectedCollectionIds.length > 0,
                })
                // The capsule in the input row IS the on-state control, so
                // the glyph would be a second place showing the same switch.
                // It stays for OFF and UNAVAILABLE: without it there is no
                // way back on except typing `/wissen`.
                if (state === 'hidden' || state === 'on') return null
                const reason = sourceAvailability.knowledgeReason === 'store_unreachable'
                  ? t.agent.composer.sourceReasonStoreUnreachable
                  : t.agent.composer.sourceReasonOperatorDisabled
                const stateHint = state === 'unavailable'
                  ? t.agent.composer.sourceKnowledgeUnavailable.replace('{reason}', reason)
                  : t.agent.composer.sourceKnowledgeOff
                const revealLabel = (
                  state === 'unavailable'
                    ? t.agent.composer.sourceUnavailableShort
                    : t.agent.composer.sourceOffShort
                ).replace('{source}', t.agent.composer.sourceKnowledgeLong)
                return (
                  <SourceToggle
                    hint={stateHint}
                    icon={Database}
                    label={t.agent.composer.sourceKnowledge}
                    locked={disabled}
                    onToggle={() =>
                      onKnowledgeSwitchChange?.(knowledgeSwitch === 'all' ? 'off' : 'all')}
                    revealLabel={revealLabel}
                    source="knowledge"
                    state={state}
                    switchedOn={knowledgeSwitch === 'all'}
                  />
                )
              })()}
            </div>

            <div className="agent-execution-capsule relative flex min-w-0 shrink items-center gap-0.5 overflow-visible">
              <DropdownMenu modal={false}>
                <DropdownMenuTrigger asChild>
                  <Button
                    aria-label={t.agent.composer.runSetup}
                    className="agent-run-setup-trigger h-7 min-w-0 shrink gap-1.5 rounded-md px-2 text-xs font-semibold text-muted-foreground [&_svg]:size-3.5 hover:bg-accent/70 hover:text-foreground data-[state=open]:bg-accent data-[state=open]:text-foreground"
                    disabled={disabled}
                    type="button"
                    variant="ghost"
                  >
                    <Workflow aria-hidden="true" className="agent-route-icon icon-sm shrink-0" />
                    <span className="agent-run-setup-label min-w-0 truncate">
                      {/* No response form here: the capsule in the input row
                          already names it, right next to this button. The
                          menu header below keeps it — it is only visible
                          while the menu is open, where the chip is not. */}
                      {runSetupSummary({ autonomy, engineMode, tierMode }, t)}
                    </span>
                  </Button>
                </DropdownMenuTrigger>
                <DropdownMenuContent
                  align="start"
                  className={cn(
                    optionMenuContentClassName,
                    'w-96 max-w-[calc(100vw-2rem)]',
                  )}
                  side="top"
                  sideOffset={8}
                >
                  <OptionMenuHeader
                    count={0}
                    title={t.agent.composer.runSetup}
                    value={runSetupSummary({ autonomy, engineMode, responseForm, tierMode }, t)}
                  />
                  <div className="py-1">
                    <p className="px-2.5 pb-1 pt-1.5 t-caption text-muted-foreground/65">
                      {t.agent.composer.autonomy}
                    </p>
                    {autonomyModes.map((mode) => (
                      <OptionMenuItem
                        active={autonomy === mode}
                        description={autonomyModeHint(mode, t)}
                        descriptionLines={2}
                        icon={mode === 'autonomous' ? ShieldCheck : Shield}
                        keepOpen
                        key={mode}
                        label={autonomyModeLabel(mode, t)}
                        onSelect={() => onAutonomyChange(mode)}
                      />
                    ))}
                    <DropdownMenuSeparator className="mx-0 my-1" />
                    <p className="px-2.5 pb-1 pt-1.5 t-caption text-muted-foreground/65">
                      {t.agent.composer.engine}
                    </p>
                    {(kernelSelectable
                      ? (['agent_kernel', 'workspace_agent'] as const)
                      : (['workspace_agent'] as const)
                    ).map((mode) => (
                      <OptionMenuItem
                        active={engineMode === mode}
                        description={
                          mode === 'agent_kernel'
                            ? t.agent.composer.engineKernelHint
                            : t.agent.composer.engineMissionHint
                        }
                        descriptionLines={2}
                        icon={mode === 'agent_kernel' ? BrainCircuit : Waypoints}
                        keepOpen
                        key={mode}
                        label={engineModeLabel(mode, t)}
                        onSelect={() => onEngineModeChange?.(mode)}
                      />
                    ))}
                    {onResponseFormChange ? (
                      <>
                        <DropdownMenuSeparator className="mx-0 my-1" />
                        {/* Submenu (opens to the side): every option inline
                            makes the main menu taller than a laptop viewport,
                            and the current value is legible on the row
                            without scrolling. */}
                        <DropdownMenuSub>
                          <DropdownMenuSubTrigger className={optionSubTriggerClassName}>
                            {(() => {
                              const Icon = responseFormIcon(responseForm)
                              return <Icon aria-hidden="true" className="icon-sm text-muted-foreground" />
                            })()}
                            <span className="min-w-0 flex-1 truncate">
                              {t.agent.composer.responseForm}
                            </span>
                            <span className="t-hint text-muted-foreground/70">
                              {responseFormLabel(responseForm, t)}
                            </span>
                          </DropdownMenuSubTrigger>
                          <DropdownMenuSubContent
                            className={cn(optionMenuContentClassName, 'w-80')}
                            sideOffset={6}
                          >
                            <p className="px-2.5 pb-1 pt-1.5 t-caption text-muted-foreground/65">
                              {t.agent.composer.responseForm}
                            </p>
                            {AGENT_RESPONSE_FORMS.map((form) => (
                              <OptionMenuItem
                                active={responseForm === form}
                                description={responseFormHint(form, t)}
                                descriptionLines={2}
                                icon={responseFormIcon(form)}
                                keepOpen
                                key={form}
                                label={responseFormLabel(form, t)}
                                onSelect={() => onResponseFormChange(form)}
                              />
                            ))}
                          </DropdownMenuSubContent>
                        </DropdownMenuSub>
                      </>
                    ) : null}
                    {tiers && tiers.length > 0 && onTierModeChange ? (
                      <>
                        <DropdownMenuSeparator className="mx-0 my-1" />
                        <DropdownMenuSub>
                          <DropdownMenuSubTrigger className={optionSubTriggerClassName}>
                            {(() => {
                              const Icon = tierIcon(tierMode ?? 'gruendlich')
                              return <Icon aria-hidden="true" className="icon-sm text-muted-foreground" />
                            })()}
                            <span className="min-w-0 flex-1 truncate">
                              {t.agent.composer.tierTitle}
                            </span>
                            <span className="t-hint text-muted-foreground/70">
                              {tierMode ? tierLabel(tierMode, t) : ''}
                            </span>
                          </DropdownMenuSubTrigger>
                          <DropdownMenuSubContent
                            className={cn(optionMenuContentClassName, 'w-80')}
                            sideOffset={6}
                          >
                            <p className="px-2.5 pb-1 pt-1.5 t-caption text-muted-foreground/65">
                              {t.agent.composer.tierTitle}
                            </p>
                            {/* No extra "no tier" entry: the composer always
                                submits the effective Stufe (selected ?? the
                                published default), so the active mark on the
                                default IS the honest state. */}
                            {tiers.map((tier) => (
                              <OptionMenuItem
                                active={tierMode === tier.id}
                                description={`${tierHint(tier.id, t)} · ${t.agent.composer.tierLatencyHint}`}
                                descriptionLines={2}
                                icon={tierIcon(tier.id)}
                                keepOpen
                                key={tier.id}
                                label={tierLabel(tier.id, t)}
                                onSelect={() => onTierModeChange(tier.id)}
                              />
                            ))}
                          </DropdownMenuSubContent>
                        </DropdownMenuSub>
                      </>
                    ) : depthSelectable && onDepthModeChange ? (
                      <>
                        <DropdownMenuSeparator className="mx-0 my-1" />
                        <p className="px-2.5 pb-1 pt-1.5 t-caption text-muted-foreground/65">
                          {t.agent.overview.rowDepth}
                        </p>
                        <OptionMenuItem
                          active={depthMode === 'normal'}
                          description={t.agent.composer.depthNormalHint}
                          descriptionLines={2}
                          icon={Gauge}
                          keepOpen
                          label={t.agent.overview.depthNormal}
                          onSelect={() => onDepthModeChange('normal')}
                        />
                        <OptionMenuItem
                          active={depthMode === 'deep'}
                          description={`${t.agent.composer.deepHint} ${t.agent.composer.deepTags}`}
                          descriptionLines={2}
                          icon={Layers}
                          keepOpen
                          label={t.agent.composer.deep}
                          onSelect={() => onDepthModeChange('deep')}
                        />
                      </>
                    ) : null}
                  </div>
                </DropdownMenuContent>
              </DropdownMenu>

              {modelPicker && (
                <ModelTierPicker
                  defaultModel={modelPicker.defaultModel}
                  disabled={disabled}
                  modelCatalog={modelPicker.catalog}
                  onChange={modelPicker.onTierChange}
                  onEffortChange={modelPicker.onEffortChange}
                  onModelChange={modelPicker.onModelChange}
                  options={modelPicker.options}
                  optionsStatus={modelPicker.optionsStatus}
                  selectedEffort={modelPicker.selectedEffort}
                  selectedModel={modelPicker.selectedModel}
                  selectedTier={modelPicker.selectedTier}
                  serverDefaultDescription={t.agent.composer.modelAutoDescription}
                  pickerTitle={t.agent.composer.modelPickerTitle}
                  serverDefaultLabel={t.agent.composer.modelAuto}
                  triggerPrefix={`${t.agent.composer.model}: `}
                  triggerVariant="agent-capsule"
                />
              )}
              <span aria-hidden="true" className="agent-execution-rail" />
            </div>
          </div>

          <div className="agent-composer-actions flex shrink-0 items-center gap-1.5">
            <span className="agent-composer-quota contents">
              <QuotaMeter disabled={disabled} />
            </span>
            {overview && (
              <AgentStatusMenu
                autonomyHint={autonomyModeHint(autonomy, t)}
                depthDeep={tierMode ? tierMode === 'tief' : depthMode === 'deep'}
                autonomyLabel={autonomyModeLabel(autonomy, t)}
                disabled={disabled}
                memoryEnabled={memoryEnabled}
                modelValue={agentModelOverviewValue(modelPicker, t)}
                overview={overview}
                responseFormValue={responseFormLabel(responseForm, t)}
                responseForm={responseForm}
                tier={tierMode}
                tiers={tiers}
                execution={statusExecution}
                sourcePolicy={sourcePolicy}
                toolUseCounts={toolUseCounts}
              />
            )}
            {running && !answerMode ? (
              <ComposerStopButton label={t.agent.composer.stop} onClick={onStop} />
            ) : (
              <Tooltip>
                <TooltipTrigger asChild>
                  <Button
                    aria-label={t.agent.composer.submit}
                    className="size-7 shrink-0 rounded-md bg-brand text-brand-foreground hover:bg-brand/90"
                    data-testid="agent-submit"
                    disabled={!canSubmit}
                    size="icon"
                    type="submit"
                  >
                    <SendHorizontal className="size-4" />
                  </Button>
                </TooltipTrigger>
                <TooltipContent>{t.agent.composer.submit}</TooltipContent>
              </Tooltip>
            )}
          </div>
        </div>
      </div>
      <ComposerDisclosureHint />
    </form>
  )
}

/** Sub-trigger row of the run-setup menu: same rhythm as an option row. */
const optionSubTriggerClassName =
  'mx-1 gap-2 rounded-md px-2 py-1.5 text-sm text-foreground [&_svg]:size-3.5 focus:bg-accent data-[state=open]:bg-accent'

/** One table for the three direct commands — icon, name and hint. The
 * `/canvas` row deliberately reuses the response form's own icon and label,
 * so the menu entry and the run-setup row read as the same thing. */
function commandIcon(id: ComposerCommandId) {
  if (id === 'web') return Globe2
  if (id === 'canvas') return responseFormIcon('canvas')
  return Database
}

function commandLabel(
  id: ComposerCommandId,
  t: ReturnType<typeof useLocale>['t'],
): string {
  if (id === 'web') return t.agent.composer.commandWeb
  if (id === 'canvas') return t.agent.composer.commandCanvas
  return t.agent.composer.commandKnowledge
}

function commandHint(
  id: ComposerCommandId,
  t: ReturnType<typeof useLocale>['t'],
): string {
  if (id === 'web') return t.agent.composer.commandWebHint
  if (id === 'canvas') return t.agent.composer.commandCanvasHint
  return t.agent.composer.commandKnowledgeHint
}

function responseFormIcon(form: AgentResponseForm) {
  if (form === 'chat') return MessageSquareText
  if (form === 'canvas') return FileText
  return WandSparkles
}

function responseFormHint(
  form: AgentResponseForm,
  t: ReturnType<typeof useLocale>['t'],
): string {
  if (form === 'chat') return t.agent.composer.responseFormChatHint
  if (form === 'canvas') return t.agent.composer.responseFormCanvasHint
  return t.agent.composer.responseFormAutoHint
}

function agentModelOverviewValue(
  picker: AgentModelPickerProps | null,
  t: ReturnType<typeof useLocale>['t'],
): string {
  if (!picker) return ''
  if (picker.selectedModel) {
    const card = picker.catalog.find(
      (entry) => entry.model_id === picker.selectedModel,
    )?.card
    const name = card?.display_name ?? picker.selectedModel
    const effort = picker.selectedEffort
      ? ` \u00b7 ${effortLevelLabel(picker.selectedEffort)}`
      : ''
    return t.agent.overview.modelOverrideValue.replace(
      '{name}',
      `${name}${effort}`,
    )
  }
  if (picker.selectedTier) {
    return t.agent.overview.modelOverrideValue.replace(
      '{name}',
      picker.selectedTier,
    )
  }
  return t.agent.overview.modelAutoValue
}

function autonomyModeHint(
  mode: string,
  t: ReturnType<typeof useLocale>['t'],
): string {
  if (mode === 'strict') return t.agent.composer.autonomyStrictHint
  if (mode === 'autonomous') return t.agent.composer.autonomyAutonomousHint
  return t.agent.composer.autonomyBalancedHint
}

/** Stufe hint — the label lives in `runSetupLabels`. */
function tierHint(
  tier: AgentTierId,
  t: ReturnType<typeof useLocale>['t'],
): string {
  if (tier === 'schnell') return t.agent.composer.tierSchnellHint
  if (tier === 'tief') return t.agent.composer.tierTiefHint
  return t.agent.composer.tierGruendlichHint
}

function tierIcon(tier: AgentTierId) {
  if (tier === 'schnell') return Gauge
  if (tier === 'tief') return Layers
  return Search
}
