import { ListChecks } from '@/components/icons'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { OptionMenuHeader, optionMenuContentClassName } from '@/components/ui/option-menu'
import { StatusRow, SummaryGroup, type StatusRowTone } from '@/components/ui/status-summary'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { composerIconButtonClassName } from '@/features/composer/ComposerIconButton'
import type { AgentTierCapability, AgentTierId } from '@/features/researchRuns/types'
import { useLocale } from '@/i18n/LocaleProvider'
import { countLabel } from '@/i18n/plural'
import { effortLevelLabel } from '@/lib/modelCard'
import { cn } from '@/lib/utils'
import type {
  AgentOverview,
  AgentToolUseCounts,
  ApprovalRow,
} from './agentStatusOverview'
import type {
  AgentExecutionSnapshot,
  AgentSourcePolicy,
} from './executionPolicy'
import { kernelToolLabel } from './activityPresentation'
import { resolveAgentExecutionDisplay } from './executionPolicy'

/**
 * Read-only overview of what THIS run would actually do — the Agent Desk
 * counterpart of the Knowledge Desk's run overview (same icon, same
 * primitives, same tri-state honesty). Every row renders server-published
 * facts (`capabilities.agent`, published == enforced) or live composer
 * state; nothing here is a hardcoded claim.
 */

function approvalValue(
  row: ApprovalRow,
  o: ReturnType<typeof useLocale>['t']['agent']['overview'],
): { tone: StatusRowTone; value: string } {
  if (row.state === 'always') return { tone: 'success', value: o.stateAlways }
  if (row.state === 'asks') {
    return {
      tone: 'success',
      value: row.conditional ? o.stateAsksConditional : o.stateAsks,
    }
  }
  return { tone: 'muted', value: o.stateFree }
}

export function AgentStatusMenu({
  autonomyHint,
  autonomyLabel,
  depthDeep = false,
  disabled,
  memoryEnabled,
  modelValue = '',
  overview,
  responseFormValue,
  responseForm,
  tier = null,
  tiers = null,
  execution = null,
  sourcePolicy,
  toolUseCounts = { web: 0, knowledge: 0 },
}: {
  /** The selected mode's meaning, reused from the toggle tooltip. */
  autonomyHint: string
  /** Display label of the selected permission mode (header value). */
  autonomyLabel: string
  /** Live composer depth toggle: true = Deep selected. */
  depthDeep?: boolean
  disabled: boolean
  /** Account preference `enable_agent_memory` (live). */
  memoryEnabled: boolean
  /** Live model-override readout from the composer picker;
   * '' hides the row (no picker wired). */
  modelValue?: string
  overview: AgentOverview
  /** Display label of the selected response form (live). */
  responseFormValue: string
  /** Raw selected values are needed to preview directive overrides. */
  responseForm: 'auto' | 'chat' | 'canvas'
  /** Published thoroughness tier; null on legacy depth-only servers. */
  tier?: AgentTierId | null
  /** Published Stufen ladder (`capabilities.agent.tiers`): the selected
   * tier's entry carries the round caps the server enforces for it. */
  tiers?: AgentTierCapability[] | null
  execution?: AgentExecutionSnapshot | null
  sourcePolicy: AgentSourcePolicy
  /** Actual completed tool calls when task-level data is available. */
  toolUseCounts?: AgentToolUseCounts
}) {
  const { t } = useLocale()
  const o = t.agent.overview
  const display = resolveAgentExecutionDisplay({
    execution,
    selectedDepth: depthDeep ? 'deep' : 'normal',
    selectedMode: overview.brain,
    selectedResponseForm: responseForm,
  })
  const acceptedExecution = execution
  const effectiveSourcePolicy = acceptedExecution?.sourcePolicy ?? sourcePolicy
  const effectiveMode = display.effectiveMode
  const effectiveResponseForm = display.responseForm === 'chat'
    ? t.agent.composer.responseFormChat
    : display.responseForm === 'canvas'
      ? t.agent.composer.responseFormCanvas
      : display.responseForm === 'auto'
        ? t.agent.composer.responseFormAuto
        : responseFormValue
  const effectiveModel = acceptedExecution
    ? acceptedExecution.model
      ? `${acceptedExecution.model}${acceptedExecution.reasoningEffort
        ? ` · ${effortLevelLabel(acceptedExecution.reasoningEffort)}`
        : ''}`
      : o.modelAutoValue
    : modelValue
  const effectiveDepthDeep = display.depth === 'deep'
  const effectiveToolUseCounts = acceptedExecution?.toolUseCounts ?? toolUseCounts
  const routeValue = effectiveMode === 'agent_kernel'
    ? o.routeAutomatic
    : o.brainMission
  const kernelLimitKey = tier === 'schnell'
    ? 'schnell'
    : effectiveDepthDeep
      ? 'deep'
      : 'normal'
  const depthLimits = overview.limits?.kernel[kernelLimitKey]
  // The selected Stufe caps clarification rounds below the operator
  // ceiling (`effective_clarification_rounds` on the server); before a run
  // publishes its live limit, the tier's entry is the enforced number.
  // Without a tier the operator ceiling is the enforced one.
  const tierEntry = tier ? tiers?.find((entry) => entry.id === tier) : undefined
  const tokenLimits = overview.limits?.tokens
  const liveLimits = acceptedExecution?.limits ?? {}
  const limitValue = (
    key: string,
    fallback: { limit: number; ceiling?: number } | null,
  ): string => {
    const live = liveLimits[key]
    if (live) {
      if (live.used !== null) {
        return (live.extendable ? o.limitUsageExtendable : o.limitUsageHard)
          .replace('{used}', String(live.used))
          .replace('{limit}', String(live.limit))
          .replace('{ceiling}', String(live.ceiling))
      }
      return live.extendable
        ? o.limitExtendable
          .replace('{limit}', String(live.limit))
          .replace('{ceiling}', String(live.ceiling))
        : o.limitHard.replace('{limit}', String(live.limit))
    }
    if (!fallback) return o.limitUnavailable
    const ceiling = Math.max(fallback.limit, fallback.ceiling ?? fallback.limit)
    return ceiling > fallback.limit
      ? o.limitExtendable
        .replace('{limit}', String(fallback.limit))
        .replace('{ceiling}', String(ceiling))
      : o.limitHard.replace('{limit}', String(fallback.limit))
  }

  const sourceValue = (
    id: 'web_search' | 'knowledge_search',
    available: boolean,
  ): { tone: StatusRowTone; value: string } => {
    if (!available) return { tone: 'warning', value: o.stateUnavailable }
    const source = id === 'web_search' ? 'web' : 'knowledge'
    return effectiveSourcePolicy[source] === 'disabled'
      ? { tone: 'muted', value: o.stateDisabledSession }
      : { tone: 'success', value: o.stateAvailable }
  }

  const approvalLabel = (row: ApprovalRow): string => {
    switch (row.id) {
      case 'plan':
        return o.rowPlan
      case 'web_search':
        return o.rowWebSearch
      case 'knowledge_search':
        return o.rowKnowledgeSearch
      case 'research':
        return o.rowResearch
      case 'skill_activation':
        return o.rowSkillActivation
      case 'editor_patch':
        return o.rowEditorPatch
      default:
        // An unknown gated tool surfaces under its raw name — a policy
        // addition must never silently vanish from the overview.
        return row.id
    }
  }

  return (
    <DropdownMenu modal={false}>
      <Tooltip>
        <DropdownMenuTrigger asChild>
          <TooltipTrigger asChild>
            <Button
              aria-label={o.title}
              className={composerIconButtonClassName}
              disabled={disabled}
              type="button"
              variant="ghost"
            >
              <ListChecks />
            </Button>
          </TooltipTrigger>
        </DropdownMenuTrigger>
        <TooltipContent>{o.title}</TooltipContent>
      </Tooltip>
      <DropdownMenuContent
        align="end"
        className={cn(optionMenuContentClassName, 'overflow-y-auto overflow-x-hidden')}
        side="top"
        sideOffset={8}
      >
        <OptionMenuHeader count={0} title={o.title} value={autonomyLabel} />
        <div className="py-1">
          <SummaryGroup label={o.groupExecution}>
            <StatusRow
              label={o.rowRoute}
              value={routeValue}
            />
            <StatusRow
              label={o.rowBrain}
              value={effectiveMode === 'agent_kernel' ? o.brainKernel : o.brainMission}
            />
            {/* Feature transparency: the engine is listed even when the
               deployment does not offer it — an absent picker alone would
               hide that it exists. Since the kernel ships as the default,
               "unavailable" here means a registration gate was missed;
               the reason travels in feature_status.agent_kernel. */}
            <StatusRow
              label={o.brainKernel}
              {...(overview.kernel === 'active'
                ? { tone: 'success' as const, value: o.stateActive }
                : overview.kernel === 'selectable'
                  ? { tone: 'muted' as const, value: o.stateSelectable }
                  : { tone: 'muted' as const, value: o.stateUnavailable })}
            />
            <StatusRow label={t.agent.composer.responseForm} value={effectiveResponseForm} />
            {effectiveModel && (
              <StatusRow label={o.rowModel} value={effectiveModel} />
            )}
            <StatusRow
              label={o.rowDepth}
              {...(effectiveDepthDeep
                ? { tone: 'success' as const, value: t.agent.composer.deep }
                : { tone: 'muted' as const, value: o.depthNormal })}
            />
            <StatusRow
              label={o.rowDurable}
              {...(overview.durable
                ? { tone: 'success' as const, value: o.stateOn }
                : { tone: 'warning' as const, value: o.stateVolatile })}
            />
            <StatusRow
              label={o.rowMemory}
              {...(memoryEnabled
                ? { tone: 'success' as const, value: o.stateOn }
                : { tone: 'muted' as const, value: o.stateOff })}
            />
          </SummaryGroup>
          {overview.limits ? (
            <>
              <DropdownMenuSeparator className="mx-0 my-1" />
              <SummaryGroup label={o.groupLimits}>
                {effectiveMode === 'agent_kernel' && depthLimits ? (
                  <>
                    <StatusRow
                      label={o.rowToolCalls}
                      value={limitValue('tool_calls', {
                        limit: depthLimits.tool_calls,
                        ceiling: depthLimits.tool_calls_ceiling,
                      })}
                    />
                    <StatusRow
                      label={o.rowSteps}
                      value={limitValue('steps', {
                        limit: depthLimits.steps,
                        ceiling: depthLimits.steps_ceiling,
                      })}
                    />
                  </>
                ) : (
                  <>
                    <StatusRow
                      label={o.rowDiscoveryCalls}
                      value={limitValue('discovery_tool_calls', {
                        limit: overview.limits.mission.discovery_tool_calls,
                      })}
                    />
                    <StatusRow
                      label={o.rowPlanTasks}
                      value={limitValue('plan_tasks', {
                        limit: overview.limits.mission.plan_tasks,
                      })}
                    />
                    <StatusRow
                      label={o.rowReplanRounds}
                      value={limitValue('replan_rounds', {
                        limit: overview.limits.mission.replan_rounds,
                      })}
                    />
                    <StatusRow
                      label={o.rowClarificationRounds}
                      value={limitValue('clarification_rounds', {
                        limit: tierEntry
                          ? tierEntry.clarification_rounds
                          : overview.limits.mission.clarification_rounds,
                      })}
                    />
                    <StatusRow
                      label={o.rowParallelChildren}
                      value={o.limitHard.replace(
                        '{limit}',
                        String(overview.limits.mission.parallel_children),
                      )}
                    />
                    <StatusRow
                      label={o.rowResearchRounds}
                      value={o.limitHard.replace(
                        '{limit}',
                        String(overview.limits.research.rounds),
                      )}
                    />
                  </>
                )}
                <StatusRow
                  label={o.rowTokens}
                  tone={tokenLimits?.enabled ? 'warning' : 'muted'}
                  value={tokenLimits?.enabled
                    ? limitValue('tokens', { limit: tokenLimits.limit })
                    : o.limitDisabled}
                />
              </SummaryGroup>
              <div className="px-2.5 pb-1 pt-1.5 t-meta-sm text-muted-foreground">
                {o.limitExplanation}
              </div>
            </>
          ) : null}
          {overview.approvals ? (
            <>
              <DropdownMenuSeparator className="mx-0 my-1" />
              <SummaryGroup label={o.groupApprovals}>
                {execution?.consentReason ? (
                  <StatusRow
                    label={o.rowConsentReason}
                    value={execution.consentReason.startsWith('explicit_')
                      ? o.stateThisMessage
                      : execution.consentReason}
                  />
                ) : null}
                {overview.approvals.map((row) => (
                  <StatusRow
                    key={row.id}
                    label={approvalLabel(row)}
                    {...approvalValue(row, o)}
                  />
                ))}
                {execution && execution.toolGrants.length > 0 ? (
                  <StatusRow
                    label={o.rowRunGrants}
                    tone="success"
                    value={execution.toolGrants
                      .map((tool) => kernelToolLabel(tool, t))
                      .join(', ')}
                  />
                ) : null}
              </SummaryGroup>
              <div className="px-2.5 pb-1 pt-1.5 t-meta-sm text-muted-foreground">
                {autonomyHint}
              </div>
            </>
          ) : null}
          <DropdownMenuSeparator className="mx-0 my-1" />
          <SummaryGroup label={o.groupTools}>
            {overview.tools.map((row) => (
              <StatusRow
                key={row.id}
                label={
                  row.id === 'web_search'
                    ? o.toolWebSearch
                    : row.id === 'knowledge_search'
                      ? o.toolKnowledge
                      : o.toolEditor
                }
                {...(row.id === 'editor_access'
                  ? row.available
                    ? { tone: 'success' as const, value: o.stateReady }
                    : { tone: 'warning' as const, value: o.stateUnavailable }
                  : sourceValue(row.id, row.available))}
              />
            ))}
            {effectiveToolUseCounts.web > 0 ? (
              <StatusRow
                label={o.toolWebUsed}
                tone="success"
                value={countLabel(effectiveToolUseCounts.web, o.toolUseCountOne, o.toolUseCount)}
              />
            ) : null}
            {effectiveToolUseCounts.knowledge > 0 ? (
              <StatusRow
                label={o.toolKnowledgeUsed}
                tone="success"
                value={countLabel(effectiveToolUseCounts.knowledge, o.toolUseCountOne, o.toolUseCount)}
              />
            ) : null}
          </SummaryGroup>
        </div>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
