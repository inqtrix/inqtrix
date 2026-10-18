import { Bot, BrainCog, Check, ChevronDown, Info, Server } from '@/components/icons'
import { Button } from '@/components/ui/button'
import { Chip } from '@/components/ui/chip'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { useLocale } from '@/i18n/LocaleProvider'
import {
  capabilityLabel,
  costTier,
  effortLevelLabel,
  formatTokens,
  modelCardDescription,
  speedLabel,
} from '@/lib/modelCard'
import { cn } from '@/lib/utils'
import { ReasoningEffortControl } from './ReasoningEffortControl'
import type {
  ChatModelOption,
  ChatModelTier,
  ModelCard,
  ModelCatalogEntry,
  ModelCardCategory,
  NodeModelResolution,
} from './types'
import {
  catalogSelectionKind,
  modelDetailLabel,
  modelEffortLabel,
  modelNameLabel,
  modelTierDescription,
  modelTierLabel,
} from './modelLabels'

const modelTierOrder: ChatModelTier[] = ['high', 'mid', 'fast']

type ModelTierPickerProps = {
  defaultModel: NodeModelResolution | null
  disabled: boolean
  onChange: (tier: ChatModelTier | null) => void
  options: ChatModelOption[]
  optionsStatus: 'available' | 'missing' | 'unresolved'
  selectedTier: ChatModelTier | null
  // Catalog mode (concrete model selection). When `modelCatalog` is non-empty
  // the picker shows grouped models with a per-model info card + reasoning
  // selector; otherwise it falls back to the high/mid/fast tier picker.
  modelCatalog?: ModelCatalogEntry[]
  selectedModel?: string | null
  selectedEffort?: string | null
  onModelChange?: (model: string | null) => void
  onEffortChange?: (effort: string | null) => void
  /** Copy overrides for non-chat contexts (agent composer). Defaults
   * keep the chat/editor instances byte-identical. */
  serverDefaultLabel?: string
  serverDefaultDescription?: string
  triggerPrefix?: string
  pickerTitle?: string
  /** Agent Desk keeps the same picker contents but gives the trigger a
   * responsive capsule segment that becomes icon-only in narrow composers. */
  triggerVariant?: 'default' | 'agent-capsule'
}

export function ModelTierPicker({
  defaultModel,
  disabled,
  onChange,
  options,
  optionsStatus,
  selectedTier,
  modelCatalog,
  selectedModel = null,
  selectedEffort = null,
  onModelChange,
  onEffortChange,
  serverDefaultLabel,
  serverDefaultDescription,
  triggerPrefix = '',
  pickerTitle,
  triggerVariant = 'default',
}: ModelTierPickerProps) {
  const { t } = useLocale()
  const catalogMode = (modelCatalog?.length ?? 0) > 0
  const defaultLabel = serverDefaultLabel ?? t.chat.modelServerDefault
  const defaultDescription =
    serverDefaultDescription ?? t.chat.modelServerDefaultDescription
  const title = pickerTitle ?? t.chat.modelPicker

  if (catalogMode && modelCatalog && onModelChange) {
    return (
      <CatalogPicker
        catalog={modelCatalog}
        disabled={disabled}
        onEffortChange={onEffortChange}
        onModelChange={onModelChange}
        pickerTitle={title}
        selectedEffort={selectedEffort}
        selectedModel={selectedModel}
        // Without this the catalog branch cannot tell "no preference" from
        // "a tier the account preference selected": both have a null model,
        // and the picker would claim the server default while the request
        // actually carries the tier.
        selectedTier={selectedTier}
        serverDefaultDescription={defaultDescription}
        serverDefaultLabel={defaultLabel}
        triggerPrefix={triggerPrefix}
        triggerVariant={triggerVariant}
      />
    )
  }

  const selectedOption = selectedTier ? modelOptionForTier(options, selectedTier) : null
  const activeModel = selectedOption ?? defaultModel ?? modelOptionForTier(options, 'mid') ?? null
  const unavailableLabel = optionsStatus === 'unresolved'
    ? t.chat.modelMetadataMissing
    : t.chat.modelDiscoveryMissing
  const activeLabel = selectedTier && optionsStatus !== 'available'
    ? `${modelTierLabel(selectedTier, t.chat)} · ${unavailableLabel}`
    : `${modelNameLabel(activeModel, t.chat.modelUnknown)} · ${modelEffortLabel(activeModel, t.chat)}`

  return (
    <DropdownMenu>
      <ModelPickerTrigger
        activeLabel={activeLabel}
        disabled={disabled}
        title={title}
        triggerPrefix={triggerPrefix}
        variant={triggerVariant}
      />
      <DropdownMenuContent
        align="start"
        className="w-72 max-w-[calc(100vw-2rem)] overflow-x-hidden rounded-xl p-0 shadow-lg"
        side="top"
        sideOffset={8}
      >
        <div className="flex items-center gap-1.5 border-b border-border px-2.5 py-1.5">
          <span className="t-meta-sm font-medium text-muted-foreground">{title}</span>
          <span className="ml-auto t-hint tabular-nums text-muted-foreground/50">
            {optionsStatus === 'available' ? options.length : 0}
          </span>
        </div>

        <div className="max-h-80 overflow-x-hidden overflow-y-auto py-1">
          <ModelMenuRow
            active={selectedTier == null}
            description={defaultDescription}
            detail={modelDetailLabel(defaultModel, t.chat)}
            icon="server"
            label={defaultLabel}
            onSelect={() => onChange(null)}
          />
          <DropdownMenuSeparator className="mx-0 my-1" />
          {optionsStatus === 'available' ? modelTierOrder.map((tier) => {
            const option = modelOptionForTier(options, tier)
            const description = modelTierDescription(tier, t.chat)
            return (
              <div key={tier}>
                <div className="flex items-center gap-1.5 px-2.5 pb-0.5 pt-1.5">
                  <span className={cn('size-1.5 rounded-full', tierVisual[tier].dot)} />
                  <span className="t-caption text-muted-foreground/60">{tier.toUpperCase()}</span>
                </div>
                <ModelMenuRow
                  active={selectedTier === tier}
                  description={description}
                  detail={`${modelTierLabel(tier, t.chat)} · ${description}`}
                  icon="bot"
                  label={modelNameLabel(option, t.chat.modelUnknown)}
                  onSelect={() => onChange(tier)}
                  tone={tier}
                />
              </div>
            )
          }) : (
            <DropdownMenuItem disabled className="w-full min-w-0 items-start rounded-none px-2.5 py-2">
              <span className="grid min-w-0 flex-1 text-left">
                <span className="truncate t-list text-foreground">{unavailableLabel}</span>
                <span className="truncate t-meta-sm text-muted-foreground">
                  {t.chat.modelServerDefault}
                </span>
              </span>
            </DropdownMenuItem>
          )}
        </div>

        <div className="flex items-center gap-2 border-t border-border bg-surface/40 px-2.5 py-1.5">
          <span className="t-caption text-muted-foreground/65">{t.chat.modelReasoningLabel}</span>
          <span className="ml-auto max-w-[12rem] truncate rounded-md border border-border bg-background px-2 py-0.5 t-meta-sm font-medium text-foreground">
            {modelEffortLabel(activeModel, t.chat)}
          </span>
        </div>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

// --------------------------------------------------------------------------- //
// Catalog mode: concrete model selection grouped by display category
// --------------------------------------------------------------------------- //

function CatalogPicker({
  catalog,
  disabled,
  onEffortChange,
  onModelChange,
  selectedEffort,
  selectedModel,
  selectedTier,
  pickerTitle,
  serverDefaultDescription,
  serverDefaultLabel,
  triggerPrefix,
  triggerVariant,
}: {
  catalog: ModelCatalogEntry[]
  disabled: boolean
  onEffortChange?: (effort: string | null) => void
  onModelChange: (model: string | null) => void
  selectedEffort: string | null
  selectedModel: string | null
  selectedTier: ChatModelTier | null
  pickerTitle: string
  serverDefaultDescription: string
  serverDefaultLabel: string
  triggerPrefix: string
  triggerVariant: 'default' | 'agent-capsule'
}) {
  const { t } = useLocale()
  const selectedEntry = catalog.find((entry) => entry.model_id === selectedModel) ?? null
  const selectedCard = selectedEntry?.card ?? null
  // A tier without an explicit model is the account preference at work. It
  // must be named, not shown as the server default: the request carries the
  // tier, so claiming otherwise would make a live setting invisible.
  const preferenceTier
    = catalogSelectionKind(selectedModel, selectedTier) === 'tier' ? selectedTier : null
  const baseTriggerLabel = selectedModel == null
    ? preferenceTier
      ? modelTierLabel(preferenceTier, t.chat)
      : serverDefaultLabel
    : selectedCard?.display_name ?? selectedModel
  // Surface the picked reasoning level at the composer so it can be verified
  // without re-opening the picker (only when an explicit effort is set).
  const triggerLabel = selectedEffort
    ? `${baseTriggerLabel} · ${effortLevelLabel(selectedEffort)}`
    : baseTriggerLabel

  const uncategorized = catalog.filter((entry) => entry.card == null)

  return (
    <DropdownMenu>
      <ModelPickerTrigger
        activeLabel={triggerLabel}
        disabled={disabled}
        title={pickerTitle}
        triggerPrefix={triggerPrefix}
        variant={triggerVariant}
      />
      <DropdownMenuContent
        align="start"
        className="w-80 max-w-[calc(100vw-2rem)] overflow-x-hidden rounded-xl p-0 shadow-lg"
        side="top"
        sideOffset={8}
      >
        <div className="flex items-center gap-1.5 border-b border-border px-2.5 py-1.5">
          <span className="t-meta-sm font-medium text-muted-foreground">{pickerTitle}</span>
          <span className="ml-auto t-hint tabular-nums text-muted-foreground/50">
            {catalog.length}
          </span>
        </div>

        <div className="max-h-80 overflow-x-hidden overflow-y-auto py-1">
          <ModelMenuRow
            active={selectedModel == null}
            description={
              preferenceTier ? t.chat.modelTierFromPreference : serverDefaultDescription
            }
            detail={
              preferenceTier ? t.chat.modelTierFromPreference : serverDefaultDescription
            }
            icon="server"
            label={
              preferenceTier
                ? `${serverDefaultLabel} · ${modelTierLabel(preferenceTier, t.chat)}`
                : serverDefaultLabel
            }
            onSelect={() => onModelChange(null)}
          />
          <DropdownMenuSeparator className="mx-0 my-1" />
          {modelTierOrder.map((category) => {
            const entries = catalog.filter((entry) => entry.card?.category === category)
            if (entries.length === 0) return null
            return (
              <div key={category}>
                <div className="flex items-center gap-1.5 px-2.5 pb-0.5 pt-1.5">
                  <span className={cn('size-1.5 rounded-full', tierVisual[category].dot)} />
                  <span className="t-caption text-muted-foreground/60">{category.toUpperCase()}</span>
                </div>
                {entries.map((entry) => (
                  <CatalogRow
                    active={entry.model_id === selectedModel}
                    card={entry.card as ModelCard}
                    key={entry.model_id}
                    modelId={entry.model_id}
                    onSelect={() => onModelChange(entry.model_id)}
                  />
                ))}
              </div>
            )
          })}
          {uncategorized.length > 0 ? (
            <div>
              <div className="flex items-center gap-1.5 px-2.5 pb-0.5 pt-1.5">
                <span className="size-1.5 rounded-full bg-muted-foreground/40" />
                <span className="t-caption text-muted-foreground/60">
                  {t.chat.modelUncategorized}
                </span>
              </div>
              {uncategorized.map((entry) => (
                <CatalogRow
                  active={entry.model_id === selectedModel}
                  card={null}
                  key={entry.model_id}
                  modelId={entry.model_id}
                  onSelect={() => onModelChange(entry.model_id)}
                />
              ))}
            </div>
          ) : null}
        </div>

        {selectedCard ? (
          <ReasoningEffortControl
            key={selectedModel ?? 'default'}
            label={t.chat.modelReasoningLabel}
            levels={selectedCard.reasoning_levels}
            onEffortChange={(effort) => onEffortChange?.(effort)}
            selectedEffort={selectedEffort}
          />
        ) : null}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

function ModelPickerTrigger({
  activeLabel,
  disabled,
  title,
  triggerPrefix,
  variant,
}: {
  activeLabel: string
  disabled: boolean
  title: string
  triggerPrefix: string
  variant: 'default' | 'agent-capsule'
}) {
  const accessibleLabel = `${title}: ${activeLabel}`
  const trigger = (
    <Button
      aria-label={accessibleLabel}
      className={cn(
        'h-7 min-w-0 max-w-[min(48vw,17rem)] shrink rounded-md px-1.5 text-xs font-semibold text-muted-foreground hover:bg-accent/70 hover:text-foreground focus-visible:ring-1 focus-visible:ring-ring focus-visible:ring-offset-0',
        'data-[state=open]:bg-accent data-[state=open]:text-foreground',
        variant === 'agent-capsule'
          && 'agent-model-trigger max-w-none px-2 [&_svg]:size-3.5',
      )}
      disabled={disabled}
      type="button"
      variant="ghost"
    >
      {variant === 'agent-capsule' ? (
        <BrainCog aria-hidden="true" className="agent-model-icon icon-sm shrink-0" />
      ) : null}
      <span className={cn(
        'min-w-0 truncate',
        variant === 'agent-capsule' && 'agent-model-trigger-label',
      )}>{`${triggerPrefix}${activeLabel}`}</span>
      <ChevronDown className="size-3 shrink-0 opacity-60" />
    </Button>
  )

  if (variant !== 'agent-capsule') {
    return <DropdownMenuTrigger asChild>{trigger}</DropdownMenuTrigger>
  }
  return <DropdownMenuTrigger asChild>{trigger}</DropdownMenuTrigger>
}

function CatalogRow({
  active,
  card,
  modelId,
  onSelect,
}: {
  active: boolean
  card: ModelCard | null
  modelId: string
  onSelect: () => void
}) {
  const { t } = useLocale()
  const visual = card ? tierVisual[card.category] : defaultVisual
  const label = card?.display_name ?? modelId
  const detail = card
    ? `${card.vendor} · ${modelCardDescription(card, t.chat.modelDescriptions)}`
    : t.chat.modelNoCard

  return (
    <DropdownMenuItem
      className={cn(
        'group relative flex w-full min-w-0 max-w-full items-center gap-2.5 rounded-none px-2.5 py-1.5 pr-1.5 text-left hover:bg-accent/50 focus:bg-accent/80 data-[highlighted]:bg-accent/80',
        active ? 'bg-accent' : 'hover:bg-accent/50',
      )}
      // Keep the menu open after picking a model so the reasoning effort can be
      // set in the same pass (preventDefault stops Radix from closing it).
      onSelect={(event) => {
        event.preventDefault()
        onSelect()
      }}
    >
      <span
        className={cn(
          'absolute inset-y-1 left-0 w-0.5 rounded-full transition-opacity group-hover:opacity-100 group-focus:opacity-100 group-data-[highlighted]:opacity-100',
          visual.bar,
          active ? 'opacity-100' : 'opacity-0',
        )}
      />
      <Bot
        className={cn(
          'icon-md shrink-0 transition-colors',
          active ? visual.icon : ['text-muted-foreground/70', visual.iconHover],
        )}
      />
      <span className="min-w-0 flex-1 overflow-hidden">
        <span className="flex items-center gap-1.5">
          <span className="block truncate t-list text-foreground">{label}</span>
          {card == null ? (
            <span className="shrink-0 rounded-sm bg-muted px-1 t-hint text-muted-foreground">
              {t.chat.modelNoCardBadge}
            </span>
          ) : null}
        </span>
        <span className="block t-meta-sm text-muted-foreground line-clamp-2">{detail}</span>
      </span>
      <ModelInfoTooltip card={card} />
      <span className="flex size-4 shrink-0 items-center justify-center">
        {active ? <Check className={cn('size-3.5', visual.check)} /> : null}
      </span>
    </DropdownMenuItem>
  )
}

function ModelInfoTooltip({ card }: { card: ModelCard | null }) {
  const { t } = useLocale()
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span
          aria-label={t.chat.modelInfo}
          className="flex size-4 shrink-0 items-center justify-center rounded-full text-muted-foreground/45 hover:text-muted-foreground"
          // Keep the row select from firing when the info affordance is used.
          onClick={(event) => event.stopPropagation()}
          onPointerDown={(event) => event.stopPropagation()}
          role="img"
        >
          <Info className="size-3.5" />
        </span>
      </TooltipTrigger>
      <TooltipContent
        className="w-80 rounded-xl border border-border bg-card p-3 text-left shadow-lg"
        side="right"
        sideOffset={8}
      >
        {card ? <ModelInfoCard card={card} /> : (
          <p className="t-meta-sm text-muted-foreground">{t.chat.modelNoCardHint}</p>
        )}
      </TooltipContent>
    </Tooltip>
  )
}

function ModelInfoCard({ card }: { card: ModelCard }) {
  const { t } = useLocale()
  const cost = costTier(card.pricing)
  return (
    <div className="grid gap-2.5">
      <div>
        <p className="t-card text-foreground">{card.display_name}</p>
        <p className="t-meta-sm text-muted-foreground">
          {card.vendor} · {card.category.toUpperCase()}-Tier
        </p>
      </div>
      <p className="t-meta-sm leading-relaxed text-muted-foreground">
        {modelCardDescription(card, t.chat.modelDescriptions)}
      </p>
      <div className="grid grid-cols-3 gap-1.5">
        <InfoTile label={t.chat.modelTileContext} value={formatTokens(card.context_window_tokens)} />
        <InfoTile label={t.chat.modelTileSpeed} value={speedLabel(card.speed)} />
        <InfoTile label={t.chat.modelTileCost} value={cost.signs} hint={cost.value} />
      </div>
      {card.capabilities.length > 0 ? (
        <div className="flex flex-wrap gap-1">
          {card.capabilities.map((capability) => (
            <Chip key={capability}>{capabilityLabel(capability)}</Chip>
          ))}
        </div>
      ) : null}
    </div>
  )
}

function InfoTile({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="grid gap-0.5 rounded-md border border-border bg-surface/50 px-2 py-1.5 text-center">
      <span className="t-caption text-muted-foreground/65">{label}</span>
      <span className="t-label tabular-nums text-foreground">{value}</span>
      {hint ? <span className="t-hint tabular-nums text-muted-foreground/70">{hint}</span> : null}
    </div>
  )
}

function ModelMenuRow({
  active,
  description,
  detail,
  icon,
  label,
  onSelect,
  tone,
}: {
  active: boolean
  description: string
  detail: string
  icon: 'bot' | 'server'
  label: string
  onSelect: () => void
  tone?: ChatModelTier
}) {
  const Icon = icon === 'server' ? Server : Bot
  const visual = tone ? tierVisual[tone] : defaultVisual

  return (
    <DropdownMenuItem
      className={cn(
        'group relative flex w-full min-w-0 max-w-full items-center gap-2.5 rounded-none px-2.5 py-1.5 pr-1.5 text-left hover:bg-accent/50 focus:bg-accent/80 data-[highlighted]:bg-accent/80',
        active ? 'bg-accent' : 'hover:bg-accent/50',
      )}
      onSelect={onSelect}
    >
      <span
        className={cn(
          'absolute inset-y-1 left-0 w-0.5 rounded-full transition-opacity group-hover:opacity-100 group-focus:opacity-100 group-data-[highlighted]:opacity-100',
          visual.bar,
          active ? 'opacity-100' : 'opacity-0',
        )}
      />
      <Icon
        className={cn(
          'icon-md shrink-0 transition-colors',
          active ? visual.icon : ['text-muted-foreground/70', visual.iconHover],
        )}
      />
      <span className="min-w-0 flex-1 overflow-hidden">
        <span className="block truncate t-list text-foreground">{label}</span>
        <span className="block truncate t-meta-sm text-muted-foreground">{detail}</span>
      </span>
      <span
        aria-hidden="true"
        className="flex size-4 shrink-0 items-center justify-center rounded-full text-muted-foreground/45"
        title={description}
      >
        <Info className="size-3.5" />
      </span>
      <span className="flex size-4 shrink-0 items-center justify-center">
        {active ? <Check className={cn('size-3.5', visual.check)} /> : null}
      </span>
    </DropdownMenuItem>
  )
}

function modelOptionForTier(
  options: readonly ChatModelOption[],
  tier: ChatModelTier,
): ChatModelOption | null {
  return options.find((option) => option.tier === tier) ?? null
}

const defaultVisual = {
  bar: 'bg-foreground',
  check: 'text-foreground',
  icon: 'text-foreground',
  iconHover: 'group-hover:text-foreground group-focus:text-foreground group-data-[highlighted]:text-foreground',
}

const tierVisual: Record<ModelCardCategory, typeof defaultVisual & { dot: string }> = {
  fast: {
    bar: 'bg-success',
    check: 'text-success',
    dot: 'bg-success',
    icon: 'text-success',
    iconHover: 'group-hover:text-success group-focus:text-success group-data-[highlighted]:text-success',
  },
  high: {
    bar: 'bg-brand',
    check: 'text-brand',
    dot: 'bg-brand',
    icon: 'text-brand',
    iconHover: 'group-hover:text-brand group-focus:text-brand group-data-[highlighted]:text-brand',
  },
  mid: {
    bar: 'bg-file',
    check: 'text-file',
    dot: 'bg-file',
    icon: 'text-file',
    iconHover: 'group-hover:text-file group-focus:text-file group-data-[highlighted]:text-file',
  },
}
