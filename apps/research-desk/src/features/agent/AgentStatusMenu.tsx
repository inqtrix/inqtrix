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
import type { AgentTierId } from '@/features/researchRuns/types'
import { useLocale } from '@/i18n/LocaleProvider'
import { effortLevelLabel } from '@/lib/modelCard'
import { cn } from '@/lib/utils'
import type {
  AgentOverview,
  ApprovalRow,
} from './agentStatusOverview'
import type {
  AgentExecutionSnapshot,
  AgentSourcePolicy,
} from './executionPolicy'
import { kernelToolLabel } from './activityPresentation'
import { resolveAgentExecutionDisplay } from './executionPolicy'
import type { AgentRunRecord } from './model'
import { knowledgeUseRow, webUseRow } from './runPresentation'

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

/** The kernel gate reason in words; an unknown code stays unnamed rather than raw. */
function kernelReasonText(
  reasons: Record<string, string>,
  reason: string | null,
): string | undefined {
  return reason ? reasons[reason] : undefined
}

export function AgentStatusMenu({
  autonomyHint,
  autonomyLabel,
  childRuns,
  depthDeep = false,
  disabled,
  memoryEnabled,
  modelValue = '',
  overview,
  responseFormValue,
  responseForm,
  tier = null,
  execution = null,
  sourcePolicy,
}: {
  /** The selected mode's meaning, reused from the toggle tooltip. */
  autonomyHint: string
  /** Display label of the selected permission mode (header value). */
  autonomyLabel: string
  /** The accepted run's delegated children: a research child reports the
   * web searches it ran, which the "web used" row counts and names. */
  childRuns?: AgentRunRecord['children']
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
  /** The execution snapshot of the session's latest run. */
  execution?: AgentExecutionSnapshot | null
  sourcePolicy: AgentSourcePolicy
}) {
  const { t } = useLocale()
  const o = t.agent.overview
  // Only a kernel run is shown as what it did. A stored run of the retired
  // mission engine is not a kernel run: its model, depth and spend would
  // read as the kernel's, so the menu shows what the next run would do.
  const acceptedExecution = execution?.effectiveMode === 'agent_kernel' ? execution : null
  const display = resolveAgentExecutionDisplay({
    execution: acceptedExecution,
    selectedDepth: depthDeep ? 'deep' : 'normal',
    selectedResponseForm: responseForm,
  })
  const effectiveSourcePolicy = acceptedExecution?.sourcePolicy ?? sourcePolicy
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
  // What the run did with each source, counted where its settings line
  // counts; before a run there is nothing to count.
  const webUsed = acceptedExecution ? webUseRow(acceptedExecution, childRuns, t) : null
  const knowledgeUsed = acceptedExecution ? knowledgeUseRow(acceptedExecution, t) : null
  const kernelLimitKey = tier === 'schnell'
    ? 'schnell'
    : effectiveDepthDeep
      ? 'deep'
      : 'normal'
  const depthLimits = overview.limits?.kernel[kernelLimitKey]
  const tokenLimits = overview.limits?.tokens
  const liveLimits = acceptedExecution?.limits ?? {}
  // One limit row: the numbers in the pill, the words on the line under it.
  // The pill is narrow and cuts what does not fit, so a row that carried
  // both lost the ceiling a limit can be extended to.
  const limitRow = (
    key: string,
    fallback: { limit: number; ceiling?: number } | null,
  ): { hint?: string; value: string } => {
    const live = liveLimits[key]
    if (live) {
      const used = live.used !== null
      return {
        hint: (live.extendable
          ? (used ? o.limitHintUsedExtendable : o.limitHintExtendable)
          : (used ? o.limitHintUsedHard : o.limitHintHard)
        ).replace('{ceiling}', String(live.ceiling)),
        value: used ? `${live.used} / ${live.limit}` : String(live.limit),
      }
    }
    if (!fallback) return { value: o.limitUnavailable }
    const ceiling = Math.max(fallback.limit, fallback.ceiling ?? fallback.limit)
    return {
      hint: ceiling > fallback.limit
        ? o.limitHintExtendable.replace('{ceiling}', String(ceiling))
        : o.limitHintHard,
      value: String(fallback.limit),
    }
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
              value={o.routeAutomatic}
            />
            <StatusRow
              label={o.rowBrain}
              value={o.brainKernel}
            />
            {/* Feature transparency: the engine is listed even when the
               deployment does not offer it. "Unavailable" means the
               operator turned the kernel off or a registration gate was
               missed; the reason travels in feature_status.agent_kernel. */}
            <StatusRow
              label={o.brainKernel}
              {...(overview.kernel === 'active'
                ? { tone: 'success' as const, value: o.stateActive }
                : { tone: 'muted' as const, value: o.stateUnavailable })}
              hint={kernelReasonText(o.kernelReasons, overview.kernelReason)}
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
                {depthLimits ? (
                  <>
                    <StatusRow
                      label={o.rowToolCalls}
                      {...limitRow('tool_calls', {
                        limit: depthLimits.tool_calls,
                        ceiling: depthLimits.tool_calls_ceiling,
                      })}
                    />
                    <StatusRow
                      label={o.rowSteps}
                      {...limitRow('steps', {
                        limit: depthLimits.steps,
                        ceiling: depthLimits.steps_ceiling,
                      })}
                    />
                  </>
                ) : null}
                <StatusRow
                  label={o.rowTokens}
                  tone={tokenLimits?.enabled ? 'warning' : 'muted'}
                  {...(tokenLimits?.enabled
                    ? limitRow('tokens', { limit: tokenLimits.limit })
                    : { value: o.limitDisabled })}
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
            {webUsed ? (
              <StatusRow label={o.toolWebUsed} tone="success" {...webUsed} />
            ) : null}
            {knowledgeUsed ? (
              <StatusRow label={o.toolKnowledgeUsed} tone="success" {...knowledgeUsed} />
            ) : null}
          </SummaryGroup>
        </div>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
