import { useEffect, useId, useMemo, useRef, useState } from 'react'
import {
  Download,
  Info,
  Plus,
  Save,
  Search,
  Sparkles,
  Trash2,
  Upload,
  Users,
  X,
} from '@/components/icons'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { ScrollArea } from '@/components/ui/scroll-area'
import { Textarea } from '@/components/ui/textarea'
import {
  TextImproveButton,
  TextImproveFieldLayer,
  useTextImprovement,
  type TextImprovementApiOptions,
} from '@/features/textImprove'
import { useLocale } from '@/i18n/LocaleProvider'
import { cn } from '@/lib/utils'
import {
  MAX_POINT_OPTIONS,
  MAX_SKILL_POINTS,
  SKILL_ALLOWED_TOOL_OPTIONS,
  canDeleteSkill,
  canEditSkill,
  emptySkillPayload,
  emptySkillPoint,
  extractPlaceholders,
  payloadFromSkill,
  scaffoldPoints,
  uncoveredPlaceholders,
  type SkillInfo,
  type SkillPayload,
  type SkillPointInfo,
} from './skillLibrary'
import type { SkillsApiHandle } from './useSkillsApi'

/**
 * Master/detail editor of the skill library — the
 * second PromptLibrary tab. Server-first (no local/synced dual state):
 * every save round-trips through `/v1/skills`, validation errors and
 * conflicts surface verbatim. The point editor stays COUPLED to the
 * `{{name}}` placeholders in the instructions (scaffolded rows), and
 * uncovered placeholders warn before the server would 400.
 */

type Draft = {
  baseRevision: number
  payload: SkillPayload
  selectedId: string | null
  dirty: boolean
  error: string
}

function emptyDraft(): Draft {
  return {
    payload: emptySkillPayload(),
    selectedId: null,
    baseRevision: 0,
    dirty: false,
    error: '',
  }
}

export function SkillLibraryPanel({
  api,
  onShare,
  onRequestedSkillHandled,
  reduceMotion = false,
  requestedSkillId = null,
  textImprovement = null,
}: {
  api: SkillsApiHandle
  onShare?: (skill: SkillInfo) => void
  onRequestedSkillHandled?: () => void
  reduceMotion?: boolean
  requestedSkillId?: string | null
  /** TextImprove wiring for the instructions field; null hides it. */
  textImprovement?: Omit<TextImprovementApiOptions, 'locale'> | null
}) {
  const fieldIds = useId()
  const { locale, t } = useLocale()
  const s = t.skills
  const [query, setQuery] = useState('')
  const [draft, setDraft] = useState<Draft>(emptyDraft)
  const importInputRef = useRef<HTMLInputElement>(null)
  const instructionsImprove = useTextImprovement({
    ...(textImprovement ?? { enabled: false, workspaceId: '' }),
    locale,
    messages: {
      requestFailed: (message) => `${t.textImprove.requestFailed}: ${message}`,
      sensitiveText: t.textImprove.sensitiveText,
      unavailable: t.textImprove.unavailable,
    },
  })

  const selected = useMemo(
    () => api.skills.find((skill) => skill.id === draft.selectedId) ?? null,
    [api.skills, draft.selectedId],
  )
  const sourceUnavailable = draft.selectedId !== null && selected === null
  const remoteConflict = Boolean(
    draft.dirty
    && draft.selectedId
    && selected
    && selected.revision !== draft.baseRevision,
  )
  const permissionDowngraded = Boolean(
    draft.dirty && selected && !canEditSkill(selected),
  )
  const editable = draft.selectedId === null
    ? true
    : selected !== null && canEditSkill(selected) && !remoteConflict
  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase()
    if (!needle) return api.skills
    return api.skills.filter(
      (skill) =>
        skill.label.includes(needle)
        || skill.title.toLowerCase().includes(needle)
        || skill.description.toLowerCase().includes(needle),
    )
  }, [api.skills, query])

  const points = useMemo(
    () =>
      scaffoldPoints(
        draft.payload.instructions_markdown,
        draft.payload.clarification_points,
      ),
    [draft.payload.clarification_points, draft.payload.instructions_markdown],
  )
  const placeholderNames = useMemo(
    () => extractPlaceholders(draft.payload.instructions_markdown),
    [draft.payload.instructions_markdown],
  )
  const uncovered = uncoveredPlaceholders(
    draft.payload.instructions_markdown,
    points,
  )

  const load = (skill: SkillInfo | null) => {
    setDraft(
      skill
        ? {
          payload: payloadFromSkill(skill),
          selectedId: skill.id,
          baseRevision: skill.revision,
          dirty: false,
          error: '',
        }
        : emptyDraft(),
    )
  }

  useEffect(() => {
    if (!draft.selectedId || draft.dirty) return
    if (!selected) {
      load(null)
      return
    }
    if (selected.revision !== draft.baseRevision) load(selected)
  }, [draft.baseRevision, draft.dirty, draft.selectedId, selected])

  useEffect(() => {
    if (!requestedSkillId) return
    const requested = api.skills.find((skill) => skill.id === requestedSkillId)
    if (!requested) return
    load(requested)
    onRequestedSkillHandled?.()
  }, [api.skills, requestedSkillId])

  const patch = (changes: Partial<SkillPayload>) => {
    setDraft((current) => ({
      ...current,
      payload: { ...current.payload, ...changes },
      dirty: true,
    }))
  }

  const patchPoint = (index: number, changes: Partial<SkillPointInfo>) => {
    const next = points.map((point, i) =>
      i === index ? { ...point, ...changes } : point)
    patch({ clarification_points: next })
  }

  const save = async () => {
    const payload: SkillPayload = {
      ...draft.payload,
      clarification_points: points.filter((point) =>
        point.question.trim() || point.name),
    }
    const saved = draft.selectedId
      ? await api.update(draft.selectedId, payload, draft.baseRevision)
      : await api.create(payload)
    if (saved) {
      load(saved)
    } else {
      setDraft((current) => ({ ...current, error: api.error || s.saveFailed }))
    }
  }

  const remove = async () => {
    if (!draft.selectedId) return
    if (await api.remove(draft.selectedId)) load(null)
  }

  const keepAsCopy = () => {
    setDraft((current) => ({
      ...current,
      baseRevision: 0,
      dirty: true,
      error: '',
      payload: {
        ...current.payload,
        label: `${current.payload.label.replace(/-copy$/, '')}-copy`,
      },
      selectedId: null,
    }))
  }

  const discardRemoteConflict = () => load(selected)

  const importFile = async (file: File) => {
    const record = await api.importMarkdown(await file.text())
    if (record) load(record)
  }

  const exportSelected = async () => {
    if (!selected) return
    const text = await api.exportMarkdown(selected.id)
    if (text == null) return
    const url = URL.createObjectURL(
      new Blob([text], { type: 'text/markdown' }),
    )
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = `${selected.label}.skill.md`
    document.body.appendChild(anchor)
    anchor.click()
    anchor.remove()
    URL.revokeObjectURL(url)
  }

  const improveInstructions = async () => {
    setDraft((current) => ({ ...current, error: '' }))
    try {
      await instructionsImprove.improve(
        'prompt_template',
        draft.payload.instructions_markdown,
        s.improveGuidance,
      )
    } catch (cause) {
      setDraft((current) => ({
        ...current,
        error: cause instanceof Error ? cause.message : String(cause),
      }))
    }
  }

  return (
    <div className="grid min-h-0 flex-1 lg:grid-cols-[minmax(260px,340px)_minmax(0,1fr)]">
      <aside className="flex min-h-0 min-w-0 flex-col border-b border-border bg-surface/50 lg:border-b-0 lg:border-r">
        <div className="border-b border-border p-4">
          <div className="flex items-center justify-between gap-3">
            <div className="flex min-w-0 items-center gap-2.5">
              <span className="grid size-9 place-items-center rounded-lg border border-success/20 bg-success-subtle text-success">
                <Sparkles className="size-4" />
              </span>
              <div className="min-w-0">
                <h1 className="t-title flex items-center gap-2 truncate text-foreground">
                  {s.title}
                  <Badge variant="outline">Beta</Badge>
                </h1>
                <p className="t-meta truncate text-muted-foreground">{s.subtitle}</p>
              </div>
            </div>
            <div className="flex shrink-0 items-center gap-1">
              <DropdownMenu modal={false}>
                <DropdownMenuTrigger asChild>
                  <Button aria-label={s.infoTitle} className="size-8" size="icon" type="button" variant="ghost">
                    <Info className="size-4" />
                  </Button>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="end" className="w-96 space-y-2 p-3 text-sm" side="bottom">
                  <p className="t-label text-foreground">{s.infoTitle}</p>
                  <p className="t-meta text-muted-foreground">{s.infoWhat}</p>
                  <p className="t-meta text-muted-foreground">{s.infoInvoke}</p>
                  <p className="t-meta text-muted-foreground">{s.infoPlaceholders}</p>
                  <p className="t-meta text-muted-foreground">{s.infoModel}</p>
                  <p className="t-meta text-muted-foreground">{s.infoSharing}</p>
                </DropdownMenuContent>
              </DropdownMenu>
              <input
                accept=".md,text/markdown"
                className="hidden"
                onChange={(event) => {
                  const file = event.target.files?.[0]
                  event.target.value = ''
                  if (file) void importFile(file)
                }}
                ref={importInputRef}
                type="file"
              />
              <Button
                aria-label={s.importSkill}
                className="size-8"
                disabled={!api.transferEnabled}
                onClick={() => importInputRef.current?.click()}
                size="icon"
                title={api.transferEnabled ? s.importSkill : s.transferDemoHint}
                type="button"
                variant="ghost"
              >
                <Upload className="size-4" />
              </Button>
              <Button aria-label={s.newSkill} className="size-8" onClick={() => load(null)} size="icon" type="button" variant="outline">
                <Plus className="size-4" />
              </Button>
            </div>
          </div>
          <label className="mt-3 flex items-center gap-2 rounded-md border border-border bg-background px-2 focus-within:ring-2 focus-within:ring-ring">
            <Search className="size-4 shrink-0 text-muted-foreground" />
            <input
              className="min-w-0 flex-1 border-0 bg-transparent py-1.5 text-sm text-foreground outline-none"
              onChange={(event) => setQuery(event.target.value)}
              placeholder={s.searchPlaceholder}
              value={query}
            />
          </label>
        </div>
        <ScrollArea className="min-h-0 flex-1">
          <div className="space-y-1.5 p-2">
            {api.error && (
              <p className="rounded-md bg-warning-subtle px-2.5 py-2 t-meta text-warning">{api.error}</p>
            )}
            {filtered.length === 0 && !api.loading ? (
              <div className="space-y-2 rounded-lg border border-dashed border-border p-4">
                <p className="t-label text-foreground">{s.emptyTitle}</p>
                <p className="t-meta text-muted-foreground">{s.emptyBody}</p>
                <pre className="overflow-x-auto rounded-md bg-surface px-2.5 py-2 text-xs text-muted-foreground">{s.emptyExample}</pre>
                <Button onClick={() => load(null)} size="sm" type="button" variant="outline">
                  <Plus className="size-4" />
                  {s.newSkill}
                </Button>
              </div>
            ) : (
              filtered.map((skill) => (
                <button
                  className={cn(
                    'w-full rounded-lg border px-3 py-2 text-left transition-colors',
                    skill.id === draft.selectedId
                      ? 'border-success/40 bg-success-subtle/50'
                      : 'border-transparent hover:bg-surface',
                  )}
                  key={skill.id}
                  onClick={() => load(skill)}
                  type="button"
                >
                  <span className="flex items-center gap-2">
                    <span className="t-label truncate text-foreground">/{skill.label}</span>
                    {skill.access.mode === 'shared' && (
                      <Badge variant="outline">
                        <Users className="mr-1 size-3" />
                        {s.sharedIn}
                      </Badge>
                    )}
                  </span>
                  <span className="t-meta line-clamp-1 text-muted-foreground">
                    {skill.description || skill.title}
                  </span>
                </button>
              ))
            )}
          </div>
        </ScrollArea>
      </aside>

      <section className="flex min-h-0 min-w-0 flex-col">
        <div className="flex items-center justify-between gap-3 border-b border-border p-4">
          <div className="min-w-0">
            <h2 className="t-title truncate text-foreground">
              {draft.selectedId ? `/${draft.payload.label}` : s.newSkill}
            </h2>
            <p className="t-meta text-muted-foreground">{s.detailSubtitle}</p>
          </div>
          <div className="flex shrink-0 items-center gap-1.5">
            {selected && (
              <Button
                aria-label={s.exportSkill}
                disabled={!api.transferEnabled}
                onClick={() => void exportSelected()}
                size="icon"
                title={api.transferEnabled ? s.exportSkill : s.transferDemoHint}
                type="button"
                variant="ghost"
              >
                <Download className="size-4" />
              </Button>
            )}
            {selected && canDeleteSkill(selected) && onShare && (
              <Button aria-label={t.sharing.share} onClick={() => onShare?.(selected)} size="icon" type="button" variant="ghost">
                <Users className="size-4" />
              </Button>
            )}
            {selected && canDeleteSkill(selected) && (
              <Button aria-label={s.delete} onClick={() => void remove()} size="icon" type="button" variant="ghost">
                <Trash2 className="size-4" />
              </Button>
            )}
            <Button disabled={!editable || !draft.dirty || !api.writable} onClick={() => void save()} size="sm" type="button">
              <Save className="size-4" />
              {s.save}
            </Button>
          </div>
        </div>
        <ScrollArea className="min-h-0 flex-1">
          <div className="mx-auto w-full max-w-3xl space-y-4 p-4">
            {draft.error && (
              <p className="rounded-md bg-warning-subtle px-2.5 py-2 t-meta text-warning">{draft.error}</p>
            )}
            {(sourceUnavailable || remoteConflict || permissionDowngraded) && (
              <div className="rounded-md border border-warning/25 bg-warning-subtle px-3 py-2.5">
                <p className="t-label text-warning">
                  {sourceUnavailable
                    ? s.sourceUnavailable
                    : permissionDowngraded
                      ? s.permissionDowngraded
                      : s.remoteConflict}
                </p>
                <p className="mt-1 t-meta text-muted-foreground">
                  {sourceUnavailable
                    ? s.sourceUnavailableHint
                    : permissionDowngraded
                      ? s.permissionDowngradedHint
                      : s.remoteConflictHint}
                </p>
                <div className="mt-2 flex flex-wrap gap-2">
                  <Button onClick={keepAsCopy} size="sm" type="button" variant="outline">
                    {s.keepAsCopy}
                  </Button>
                  <Button onClick={discardRemoteConflict} size="sm" type="button" variant="ghost">
                    {s.discardDraft}
                  </Button>
                </div>
              </div>
            )}
            <div className="grid gap-3 sm:grid-cols-2">
              <Field htmlFor={`${fieldIds}-label`} label={s.fieldLabel}>
                <Input
                  id={`${fieldIds}-label`}
                  className="h-8"
                  disabled={!editable}
                  onChange={(event) =>
                    patch({
                      label: event.target.value
                        .toLowerCase()
                        .replace(/[^a-z0-9-]/g, '-'),
                    })}
                  placeholder="sprechzettel"
                  value={draft.payload.label}
                />
              </Field>
              <Field htmlFor={`${fieldIds}-title`} label={s.fieldTitle}>
                <Input
                  id={`${fieldIds}-title`}
                  className="h-8"
                  disabled={!editable}
                  onChange={(event) => patch({ title: event.target.value })}
                  value={draft.payload.title}
                />
              </Field>
            </div>
            <Field hint={s.fieldDescriptionHint} htmlFor={`${fieldIds}-description`} label={s.fieldDescription}>
              <Input
                id={`${fieldIds}-description`}
                className="h-8"
                disabled={!editable}
                onChange={(event) => patch({ description: event.target.value })}
                value={draft.payload.description}
              />
            </Field>
            <Field hint={s.fieldWhenToUseHint} htmlFor={`${fieldIds}-when-to-use`} label={s.fieldWhenToUse}>
              <Input
                id={`${fieldIds}-when-to-use`}
                className="h-8"
                disabled={!editable}
                onChange={(event) => patch({ when_to_use: event.target.value })}
                value={draft.payload.when_to_use}
              />
            </Field>
            <Field hint={s.fieldInstructionsHint} htmlFor={`${fieldIds}-instructions`} label={s.fieldInstructions}>
              <div className="overflow-hidden rounded-md border border-border bg-background focus-within:ring-2 focus-within:ring-ring">
                {textImprovement && (
                  <div className="flex items-center justify-end border-b border-border bg-surface/50 px-2 py-1">
                    <TextImproveButton
                      disabled={
                        !editable
                        || !draft.payload.instructions_markdown.trim()
                        || !textImprovement.enabled
                      }
                      isLoading={instructionsImprove.isImproving}
                      label={t.textImprove.improve}
                      loadingLabel={t.textImprove.improving}
                      onClick={() => void improveInstructions()}
                      reduceMotion={reduceMotion}
                    />
                  </div>
                )}
                <div className="relative min-w-0">
                  <Textarea
                    id={`${fieldIds}-instructions`}
                    className="min-h-40 rounded-none border-0 font-mono text-sm shadow-none focus-visible:ring-0"
                    disabled={!editable}
                    onChange={(event) =>
                      patch({ instructions_markdown: event.target.value })}
                    value={draft.payload.instructions_markdown}
                  />
                  <TextImproveFieldLayer
                    labels={{
                      accept: t.textImprove.accept,
                      changes: t.textImprove.changes,
                      noChanges: t.textImprove.noChanges,
                      reject: t.textImprove.reject,
                      title: t.textImprove.title,
                      warnings: t.textImprove.warnings,
                    }}
                    onAccept={(instructions_markdown) => {
                      patch({ instructions_markdown })
                      instructionsImprove.clearProposal()
                    }}
                    onReject={instructionsImprove.clearProposal}
                    proposal={instructionsImprove.proposal}
                    reduceMotion={reduceMotion}
                  />
                </div>
              </div>
            </Field>

            <div className="space-y-2">
              <div className="flex items-center justify-between">
                <p className="t-label text-foreground">{s.pointsTitle}</p>
                <Button
                  disabled={!editable || points.length >= MAX_SKILL_POINTS}
                  onClick={() =>
                    patch({
                      clarification_points: [...points, emptySkillPoint()],
                    })}
                  size="sm"
                  type="button"
                  variant="ghost"
                >
                  <Plus className="size-4" />
                  {s.pointAdd}
                </Button>
              </div>
              <p className="t-meta text-muted-foreground">{s.pointsHint}</p>
              {uncovered.length > 0 && (
                <p className="rounded-md bg-warning-subtle px-2.5 py-2 t-meta text-warning">
                  {s.pointsUncovered.replace('{names}', uncovered.join(', '))}
                </p>
              )}
              {points.map((point, index) => (
                <div className="space-y-2 rounded-lg border border-border p-3" key={`${point.name || 'frei'}-${index}`}>
                  <div className="flex items-center justify-between gap-2">
                    <span className="t-meta font-medium text-foreground">
                      {point.name
                        ? `{{${point.name}}}${
                          placeholderNames.includes(point.name)
                            ? ''
                            : ` · ${s.pointOrphaned}`
                        }`
                        : s.pointFree}
                    </span>
                    <div className="flex items-center gap-2">
                      <label className="flex items-center gap-1.5 t-meta text-muted-foreground">
                        <input
                          checked={point.required}
                          disabled={!editable}
                          onChange={(event) =>
                            patchPoint(index, { required: event.target.checked })}
                          type="checkbox"
                        />
                        {s.pointRequired}
                      </label>
                      {(!point.name
                        || !placeholderNames.includes(point.name)) && (
                        <Button
                          aria-label={s.pointRemove}
                          className="size-7"
                          disabled={!editable}
                          onClick={() =>
                            patch({
                              clarification_points: points.filter(
                                (_item, i) => i !== index,
                              ),
                            })}
                          size="icon"
                          type="button"
                          variant="ghost"
                        >
                          <X className="size-4" />
                        </Button>
                      )}
                    </div>
                  </div>
                  <Input
                    className="h-8"
                    disabled={!editable}
                    onChange={(event) =>
                      patchPoint(index, { question: event.target.value })}
                    placeholder={s.pointQuestionPlaceholder}
                    value={point.question}
                  />
                  <Input
                    className="h-8"
                    disabled={!editable}
                    onChange={(event) =>
                      patchPoint(index, {
                        options: event.target.value
                          .split(',')
                          .map((label) => label.trim())
                          .filter(Boolean)
                          .slice(0, MAX_POINT_OPTIONS)
                          .map((label) => ({ label })),
                      })}
                    placeholder={s.pointOptionsPlaceholder}
                    value={point.options.map((option) => option.label).join(', ')}
                  />
                  <Input
                    className="h-8"
                    disabled={!editable}
                    onChange={(event) =>
                      patchPoint(index, {
                        default_assumption: event.target.value,
                      })}
                    placeholder={s.pointAssumptionPlaceholder}
                    value={point.default_assumption}
                  />
                </div>
              ))}
            </div>

            <div className="grid gap-3 sm:grid-cols-3">
              <Field htmlFor={`${fieldIds}-deliverable`} label={s.fieldDeliverable}>
                <select
                  id={`${fieldIds}-deliverable`}
                  className="w-full rounded-md border border-border bg-background px-2 py-1.5 text-sm text-foreground"
                  disabled={!editable}
                  onChange={(event) =>
                    patch({
                      deliverable: event.target
                        .value as SkillPayload['deliverable'],
                    })}
                  value={draft.payload.deliverable}
                >
                  <option value="">{s.deliverableAuto}</option>
                  <option value="chat">Chat</option>
                  <option value="canvas">Canvas</option>
                  <option value="email">E-Mail</option>
                  <option value="talking_points">{s.deliverableTalkingPoints}</option>
                </select>
              </Field>
              <Field hint={s.fieldRequiresPlanHint} htmlFor={`${fieldIds}-requires-plan`} label={s.fieldRequiresPlan}>
                <select
                  id={`${fieldIds}-requires-plan`}
                  className="w-full rounded-md border border-border bg-background px-2 py-1.5 text-sm text-foreground"
                  disabled={!editable}
                  onChange={(event) =>
                    patch({
                      requires_plan: event.target
                        .value as SkillPayload['requires_plan'],
                    })}
                  value={draft.payload.requires_plan}
                >
                  <option value="auto">{s.requiresPlanAuto}</option>
                  <option value="always">{s.requiresPlanAlways}</option>
                  <option value="never">{s.requiresPlanNever}</option>
                </select>
              </Field>
              <Field hint={s.fieldInvocationHint} htmlFor={`${fieldIds}-invocation`} label={s.fieldInvocation}>
                <select
                  id={`${fieldIds}-invocation`}
                  className="w-full rounded-md border border-border bg-background px-2 py-1.5 text-sm text-foreground"
                  disabled={!editable}
                  onChange={(event) =>
                    patch({
                      invocation: event.target
                        .value as SkillPayload['invocation'],
                    })}
                  value={draft.payload.invocation}
                >
                  <option value="user_only">{s.invocationUserOnly}</option>
                  <option value="model_allowed">{s.invocationModelAllowed}</option>
                </select>
              </Field>
            </div>

            <Field hint={s.fieldAllowedToolsHint} label={s.fieldAllowedTools}>
              <div className="flex flex-wrap gap-2">
                {SKILL_ALLOWED_TOOL_OPTIONS.map((toolName) => {
                  const active = draft.payload.allowed_tools.includes(toolName)
                  return (
                    <button
                      className={cn(
                        'rounded-md border px-2 py-1 t-meta transition-colors',
                        active
                          ? 'border-success/40 bg-success-subtle text-success'
                          : 'border-border text-muted-foreground hover:text-foreground',
                      )}
                      disabled={!editable}
                      key={toolName}
                      onClick={() =>
                        patch({
                          allowed_tools: active
                            ? draft.payload.allowed_tools.filter(
                              (item) => item !== toolName,
                            )
                            : [...draft.payload.allowed_tools, toolName],
                        })}
                      type="button"
                    >
                      {toolName}
                    </button>
                  )
                })}
              </div>
            </Field>

            <div className="grid gap-3 sm:grid-cols-2">
              <Field hint={s.fieldPinsHint} htmlFor={`${fieldIds}-model-tier`} label={s.fieldModelTier}>
                <select
                  id={`${fieldIds}-model-tier`}
                  className="w-full rounded-md border border-border bg-background px-2 py-1.5 text-sm text-foreground"
                  disabled={!editable}
                  onChange={(event) =>
                    patch({
                      model_tier: event.target
                        .value as SkillPayload['model_tier'],
                    })}
                  value={draft.payload.model_tier}
                >
                  <option value="">{s.pinNone}</option>
                  <option value="high">high</option>
                  <option value="mid">mid</option>
                  <option value="fast">fast</option>
                </select>
              </Field>
              <Field htmlFor={`${fieldIds}-effort`} label={s.fieldEffort}>
                <select
                  id={`${fieldIds}-effort`}
                  className="w-full rounded-md border border-border bg-background px-2 py-1.5 text-sm text-foreground"
                  disabled={!editable}
                  onChange={(event) => patch({ effort: event.target.value })}
                  value={draft.payload.effort}
                >
                  <option value="">{s.pinNone}</option>
                  <option value="minimal">minimal</option>
                  <option value="low">low</option>
                  <option value="medium">medium</option>
                  <option value="high">high</option>
                  <option value="xhigh">xhigh</option>
                </select>
              </Field>
            </div>

            <div className="grid gap-3 sm:grid-cols-2">
              <Field htmlFor={`${fieldIds}-argument-hint`} label={s.fieldArgumentHint}>
                <Input
                  id={`${fieldIds}-argument-hint`}
                  className="h-8"
                  disabled={!editable}
                  onChange={(event) =>
                    patch({ argument_hint: event.target.value })}
                  value={draft.payload.argument_hint}
                />
              </Field>
              <Field htmlFor={`${fieldIds}-autocomplete`} label={s.fieldAutocomplete}>
                <label className="flex items-center gap-2 py-1.5 t-meta text-muted-foreground">
                  <input
                    id={`${fieldIds}-autocomplete`}
                    checked={draft.payload.include_in_autocomplete}
                    disabled={!editable}
                    onChange={(event) =>
                      patch({
                        include_in_autocomplete: event.target.checked,
                      })}
                    type="checkbox"
                  />
                  {s.fieldAutocompleteHint}
                </label>
              </Field>
            </div>
          </div>
        </ScrollArea>
      </section>
    </div>
  )
}

/** A labelled form row.
 *
 * `htmlFor` binds the visible text to its control, which is what gives the
 * control an accessible name (WCAG 2.1 A, 4.1.2). Without it the text was a
 * plain paragraph and ten controls in this panel announced as unnamed —
 * the five selects worst, since their value alone says nothing.
 *
 * A row that wraps a GROUP of controls (no single target for `htmlFor`)
 * names the group instead, via `role="group"` + `aria-labelledby`.
 */
function Field({
  children,
  hint,
  htmlFor,
  label,
}: {
  children: React.ReactNode
  hint?: string
  htmlFor?: string
  label: string
}) {
  const groupLabelId = useId()
  if (htmlFor) {
    return (
      <div className="space-y-1">
        <label className="t-label block text-foreground" htmlFor={htmlFor}>
          {label}
        </label>
        {children}
        {hint && <p className="t-meta text-muted-foreground">{hint}</p>}
      </div>
    )
  }
  return (
    <div aria-labelledby={groupLabelId} className="space-y-1" role="group">
      <p className="t-label text-foreground" id={groupLabelId}>
        {label}
      </p>
      {children}
      {hint && <p className="t-meta text-muted-foreground">{hint}</p>}
    </div>
  )
}
