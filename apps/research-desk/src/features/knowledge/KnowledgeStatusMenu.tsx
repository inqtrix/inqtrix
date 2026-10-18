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
import { useLocale } from '@/i18n/LocaleProvider'
import type { KnowledgeProfileOption } from './profileOptions'
import { profileDisplayName } from './stepLines'

/**
 * Read-only overview of what the SELECTED profile actually runs — the Knowledge
 * Desk counterpart of the Research Desk's run-settings summary. Built entirely
 * from capability data the composer already holds, so it stays honest about
 * deployment degradation (a stage the profile wants but the operator removed
 * reads "unavailable", not silently off).
 */

type StageState = 'active' | 'unused' | 'degraded'

/**
 * Three honest states for one pipeline stage:
 * - `degraded`: the profile wants it but the deployment removed it (in the
 *   ceiling `degraded` list) — a warning, the user cannot get it here.
 * - `active`: the profile runs it.
 * - `unused`: the profile simply does not use it (e.g. `schnell` has no rerank)
 *   — neutral, never a warning.
 */
function stageState(enabled: boolean, degraded: readonly string[], degradedKey: string): StageState {
  if (degraded.includes(degradedKey)) return 'degraded'
  return enabled ? 'active' : 'unused'
}

function stageTone(state: StageState): StatusRowTone {
  return state === 'active' ? 'success' : state === 'degraded' ? 'warning' : 'muted'
}

export function KnowledgeStatusMenu({
  disabled,
  effectiveFinalK,
  effectiveTopK,
  finalKOverridden,
  profile,
  rerankerProvider,
  topKOverridden,
}: {
  disabled: boolean
  effectiveFinalK: number
  effectiveTopK: number
  /** Whether final_k is a manual override (vs the profile factor). */
  finalKOverridden: boolean
  /** The resolved profile that would run; null when no profile engine. */
  profile: KnowledgeProfileOption | null
  rerankerProvider: string | null
  /** Whether top_k is a manual override (vs the server default). */
  topKOverridden: boolean
}) {
  const { t } = useLocale()
  if (!profile) return null
  const k = t.knowledge
  const profileLabel = profileDisplayName(profile.id, k)
  const stages = profile.stages
  const degraded = profile.degraded

  const stageValue = (state: StageState, activeLabel: string): { tone: StatusRowTone; value: string } => ({
    tone: stageTone(state),
    value: state === 'active' ? activeLabel : state === 'degraded' ? k.stageUnavailable : k.stageOff,
  })

  // Only the override is flagged (the profile default is the implicit norm).
  const widthValue = (n: number, overridden: boolean): string =>
    overridden ? `${n} · ${k.overviewManual}` : String(n)

  return (
    <DropdownMenu modal={false}>
      <Tooltip>
        <DropdownMenuTrigger asChild>
          <TooltipTrigger asChild>
            <Button
              aria-label={k.runOverview}
              className={composerIconButtonClassName}
              disabled={disabled}
              type="button"
              variant="ghost"
            >
              <ListChecks />
            </Button>
          </TooltipTrigger>
        </DropdownMenuTrigger>
        <TooltipContent>{k.runOverview}</TooltipContent>
      </Tooltip>
      <DropdownMenuContent align="end" className={optionMenuContentClassName} side="top" sideOffset={8}>
        <OptionMenuHeader title={k.runOverview} value={profileLabel} count={0} />
        <div className="py-1">
          <SummaryGroup label={k.overviewRetrievalGroup}>
            <StatusRow label={k.topKLabel} value={widthValue(effectiveTopK, topKOverridden)} />
            {/* Concrete profiles always show final_k; `auto` (stages null) only
               when it is a manual override (its default equals top_k, factor 1),
               so the override never silently disappears from the overview. */}
            {(stages || finalKOverridden) ? (
              <StatusRow label={k.finalKLabel} value={widthValue(effectiveFinalK, finalKOverridden)} />
            ) : null}
          </SummaryGroup>
          {stages ? (
            <>
              <DropdownMenuSeparator className="mx-0 my-1" />
              <SummaryGroup label={k.overviewStagesGroup}>
                {(() => {
                  const rerank = stageState(stages.rerank, degraded, 'rerank')
                  return (
                    <StatusRow
                      label={k.overviewReranker}
                      {...stageValue(rerank, rerankerProvider ?? k.stageActive)}
                    />
                  )
                })()}
                {(() => {
                  // The ceiling degrades the gate via either 'gate' (off entirely)
                  // or 'gate_rounds' (rounds clamped below the profile's request);
                  // both must read as degraded, not silently "active".
                  const gate: StageState = degraded.includes('gate') || degraded.includes('gate_rounds')
                    ? 'degraded'
                    : stages.gateRounds > 0 ? 'active' : 'unused'
                  return (
                    <StatusRow
                      label={k.overviewGate}
                      {...stageValue(gate, String(stages.gateRounds))}
                    />
                  )
                })()}
                <StatusRow
                  label={k.overviewGrounding}
                  {...stageValue(stageState(stages.grounding, degraded, 'grounding'), k.stageActive)}
                />
                <StatusRow
                  label={k.overviewDecompose}
                  {...stageValue(stages.decompose ? 'active' : 'unused', k.stageActive)}
                />
                <StatusRow
                  label={k.overviewReport}
                  {...stageValue(stages.report ? 'active' : 'unused', k.stageActive)}
                />
                <StatusRow
                  label={k.overviewVocabulary}
                  {...stageValue(stages.vocabularyBridge ? 'active' : 'unused', k.stageActive)}
                />
              </SummaryGroup>
            </>
          ) : (
            <div className="px-2.5 pb-2 pt-1 t-meta-sm text-muted-foreground">
              {k.profileAutoDelegates.replace(
                '{profiles}',
                profile.delegatesTo.map((id) => profileDisplayName(id, k)).join(', '),
              )}
            </div>
          )}
        </div>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
