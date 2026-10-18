import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type MutableRefObject,
  type MouseEvent,
  type MouseEvent as ReactMouseEvent,
} from 'react'
import { AnimatePresence, motion, useReducedMotion } from 'motion/react'

import { appMotion } from '@/motion/transitions'

import {
  getKnowledgeChunk,
  type KnowledgeChunkDetail,
} from '@/api/inqtrixClient'
import { canvasSurfaceClass } from '@/features/canvas/CanvasSurface'
import { subscribeRunEvents } from '@/features/researchRuns/runEventChannel'
import {
  AlertTriangle,
  BookOpen,
  Check,
  ChevronLeft,
  ChevronRight,
  Clock3,
  Copy,
  Download,
  ExternalLink,
  FileText,
  Globe2,
  Info,
  LoaderCircle,
  PenLine,
  X,
} from '@/components/icons'
import { MarkdownRenderer } from '@/components/markdown/MarkdownRenderer'
import { MarkdownSelectionCopyMenu } from '@/components/markdown/MarkdownSelectionCopyMenu'
import { Button } from '@/components/ui/button'
import { PhaseSegments } from '@/components/ui/phase-segments'
import { ScrollArea } from '@/components/ui/scroll-area'
import { Skeleton } from '@/components/ui/skeleton'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import {
  StatusBadge,
  type StatusTone,
} from '@/features/settings/parts'
import type { CanvasViewDescriptor } from '@/features/canvas/types'
import { selectableRevisions } from './revisions'
import { DocumentDiffView } from '@/features/editor/DocumentDiffView'
import { editorCopy } from '@/features/editor/editorCopy'
import { MarkdownEditorSurface } from '@/features/editor/core/MarkdownEditorSurface'
import {
  FilePreviewBody,
  FilePreviewTabSwitch,
  useFilePreviewTabs,
} from '@/features/files/FilePreviewBody'
import { phaseLabel } from '@/features/researchDesk/components/runDisplay'
import { phaseOrder } from '@/features/researchDesk/types'
import type {
  ResearchRunEvent,
  ResearchRunSnapshot,
} from '@/features/researchRuns/types'
import type { FileAssetRecord } from '@/features/project/types'
import { useRunningDuration } from '@/features/researchRuns/useRunningDuration'
import {
  citationViews,
  firstOpenableCitation,
  groupCitationsByDocument,
} from '@/features/knowledge/citations'
import {
  CitationGroupList,
  CitationRow,
} from '@/features/knowledge/CitationRow'
import { DocumentReader } from '@/features/knowledge/KnowledgeSourcePanel'
import { useLocale } from '@/i18n/LocaleProvider'
import { countLabel } from '@/i18n/plural'
import { scheduleIdle } from '@/lib/idle'
import { formatDuration } from '@/lib/time'
import { withAiDisclosure } from '@/lib/aiDisclosure'
import { cn } from '@/lib/utils'
import { suggestionDiffSegments } from '@/features/editor/suggestionDiff'
import { AgentActivityLine, AgentPulseTrack } from '../AgentPulseTrack'
import {
  canEditAgentRun,
  type AgentChildProgressRecord,
  type AgentPlanTaskRecord,
  isActiveAgentRun,
  isGateAgentRun,
  TERMINAL_AGENT_TASK_STATUSES,
  type AgentRunRecord,
  resolveAgentArtifact,
} from '../model'
import { PlanReviewBody } from '../plan/PlanReviewBody'
import {
  decidedReportGuidance,
  decidedReportRuleLabels,
  gateRuleOptions,
} from '../plan/reportGuidance'
import { usePlanApproval } from '../plan/usePlanApproval'
import { usePatchReview } from '../patch/usePatchReview'
import {
  activityDisplayText,
  terminalActivityErrorIndex,
} from '../activityPresentation'
import {
  agentArtifactReferences,
  agentCitationLabelFromHref,
  agentReferenceAsKnowledge,
  agentReferenceViewerTarget,
  isAgentCitationLabel,
  linkifyAgentArtifactCitations,
  type AgentArtifactReference,
} from '../artifactCitations'
import {
  EvidenceProvenancePanel,
} from '../EvidenceProvenancePanel'
import {
  evidenceLineageFromArtifactPayload,
  safeEvidenceHttpUrl,
} from '../evidenceProvenance'
import { WebEvidenceSourceRow } from '../WebEvidenceSourceRow'
import { activityText, RunOutcomeLine } from '../timeline/AgentTimeline'
import { artifactClaimMarks, documentOutcome } from '../runPresentation'
import { ClaimNote, useClaimNote } from '../claimNote'
import {
  agentTaskExecutionLabel,
  agentTaskElapsedSeconds,
  agentTaskExecutionSemantics,
  agentTaskGroups,
  agentTaskMetrics,
  agentTaskResultContent,
  agentTaskResultPreview,
  agentTaskStatusLabel,
  effectiveAgentTaskStatus,
  type AgentTaskEffectiveStatus,
  type AgentTaskGroup,
} from '../plan/taskPresentation'
import {
  childProgressMessage,
  appendChildProgressMessage,
  completedResearchPhases,
  mergeResearchSnapshot,
  researchNodePhase,
  snapshotWithResearchMetrics,
  type ChildProgressHistory,
} from '../taskProgress'
import { useAgentCanvas } from './context'
import { prefetchableTaskResultIds } from '../taskResultPrefetch'
import type { AgentTaskResultWire } from '../types'
import {
  taskResultReferenceGroups,
} from '../taskResultReferences'
import { copyTextToClipboard } from '@/lib/clipboard'
import { createProjectEntityId } from '@/features/project/entityId'
import {
  AGENT_CANVAS_COMMENT_LIMIT,
  anchorFromMarkdownQuote,
} from './commentQueue'
import { CanvasCommentPopover } from './CanvasCommentPopover'
import {
  applyCanvasCommentHighlights,
  findQuoteRanges,
} from './commentHighlights'
import { CANVAS_SAVE_DEBOUNCE_MS, createCanvasSaveGate } from './saveGate'

// --- document ----------------------------------------------------------------

/**
 * The memo canvas (the `document` view): while the agent writes, a
 * streaming-friendly report renderer with the "agent writing" chip; once
 * ready, the ONE Tiptap surface with debounced optimistic-concurrency saves
 * (409s reload instead of overwriting).
 */
export function DocumentCanvasView({
  descriptor,
}: {
  descriptor: Extract<CanvasViewDescriptor, { view: 'document' }>
}) {
  const context = useAgentCanvas()
  const { locale, t } = useLocale()
  const reduceMotion = Boolean(useReducedMotion())
  // The descriptor's runId is a HINT: an artifact update re-anchors
  // the row to the newest run, so every read/write resolves the CURRENT
  // anchor first — a stale tab or an old turn's button keeps working.
  const resolved = resolveAgentArtifact(
    context.runs,
    context.sessionArtifacts,
    descriptor,
  )
  const effectiveRunId = resolved.runId
  const run = context.runs[effectiveRunId]
  const artifact = resolved.artifact
  const [notice, setNotice] = useState<string | null>(null)
  const [exporting, setExporting] = useState(false)
  const exportPendingRef = useRef(false)
  const [exportedFor, setExportedFor] = useState<string | null>(null)
  const exportSubject = `${descriptor.artifactId}:${artifact?.revision}:${artifact?.title}`
  const [editing, setEditing] = useState(false)
  // Inline rename: the user edits the TITLE in clear text; the
  // displayed file name follows it via the derived-name pipeline.
  const [renaming, setRenaming] = useState(false)
  // Selection-comment composer: a compact popover AT the anchor
  // (selection or highlight) — nothing sends until the next submission.
  // `editingId` re-targets the submit at an existing queue row.
  const [commentComposer, setCommentComposer] = useState<{
    anchor: { quote: string; quoteBefore: string; quoteAfter: string }
    plainText: string
    position: CSSProperties
    editingId: string | null
    initialText: string
  } | null>(null)
  const saveTimerRef = useRef<number | null>(null)
  const pendingRef = useRef<string | null>(null)
  const baseRevisionRef = useRef(0)
  const [saveGate] = useState(createCanvasSaveGate)
  // Root for the pending-comment highlight search (rendered text).
  const markdownRootRef = useRef<HTMLDivElement | null>(null)

  const queueComment = (
    anchor: { quote: string; quoteBefore: string; quoteAfter: string },
    comment: string,
    plainText: string,
    editingId: string | null = null,
  ) => {
    if (!artifact || !comment.trim()) return
    if (editingId) {
      context.updateCanvasComment(editingId, comment.trim())
      setCommentComposer(null)
      return
    }
    const accepted = context.queueCanvasComment({
      artifactId: descriptor.artifactId,
      comment: comment.trim(),
      id: createProjectEntityId('canvas-comment'),
      plainText,
      quote: anchor.quote,
      quoteAfter: anchor.quoteAfter,
      quoteBefore: anchor.quoteBefore,
      revision: artifact.revision,
    })
    if (!accepted) {
      // Visible bound (mirror of the server's max): never a silent drop.
      setNotice(
        t.agent.canvas.commentLimit.replace(
          '{count}',
          String(AGENT_CANVAS_COMMENT_LIMIT),
        ),
      )
      return
    }
    setCommentComposer(null)
  }

  const commitRename = async (value: string) => {
    setRenaming(false)
    const nextTitle = value.trim()
    if (!artifact || !nextTitle || nextTitle === artifact.title) return
    try {
      const result = await context.renameArtifact(
        effectiveRunId,
        descriptor.artifactId,
        nextTitle,
        run?.sessionId ?? null,
      )
      if (result.kind === 'locked') {
        setNotice(t.agent.canvas.lockedByAgent)
      }
    } catch {
      setNotice(t.agent.canvas.renameFailed)
    }
  }

  // The body loads on demand (the list rows never carry it). A missing
  // RECORD with a known anchor run also loads: after a re-anchor the
  // artifact reappears under the anchor run via this fetch. An anchor
  // that is no desk record (a delegated child) loads too — its body
  // lands on the session index row the resolver serves.
  // Depends on the loader and the ids only: the context changes with every
  // event of a running run, and that is no reason to ask again.
  const loadDocumentBody = context.loadArtifact
  const documentBodyMissing = artifact?.contentMarkdown === undefined
  useEffect(() => {
    if (!documentBodyMissing) return
    void loadDocumentBody(effectiveRunId, descriptor.artifactId)
  }, [descriptor.artifactId, documentBodyMissing, effectiveRunId, loadDocumentBody])

  // Queued comments keep their anchors visibly highlighted until
  // they travel (the pending convention). Repainted after every body
  // or queue change — the renderer replaces the DOM, so the paint runs
  // one frame after commit. NUL join = stable dependency key.
  const pendingHighlightKey = context.canvasComments
    .filter((draft) => draft.artifactId === descriptor.artifactId)
    .map((draft) => draft.plainText)
    .filter(Boolean)
    .join('\u0000')
  const activeHighlightText =
    commentComposer?.editingId != null ? commentComposer.plainText : null
  useEffect(() => {
    const frame = requestAnimationFrame(() => {
      applyCanvasCommentHighlights(
        markdownRootRef.current,
        pendingHighlightKey ? pendingHighlightKey.split('\u0000') : [],
        activeHighlightText,
      )
    })
    return () => cancelAnimationFrame(frame)
  }, [
    activeHighlightText,
    artifact?.contentMarkdown,
    editing,
    pendingHighlightKey,
  ])
  useEffect(
    () => () => {
      applyCanvasCommentHighlights(null, [])
    },
    [],
  )

  // ONE edit opener shared by the composer pencil channel
  // and the click-on-highlight path: scroll to the anchor, open the
  // popover prefilled. A vanished anchor still opens at a visible
  // fallback position — the edit is never silently refused.
  const openEditFor = (
    edit: (typeof context.canvasComments)[number],
    knownRange?: Range,
  ) => {
    const root = markdownRootRef.current
    const range = knownRange
      ?? (root
        ? findQuoteRanges(root, [edit.plainText || edit.quote])[0]
        : undefined)
    range?.startContainer.parentElement?.scrollIntoView({ block: 'center' })
    requestAnimationFrame(() => {
      const rect = range?.getBoundingClientRect()
      const anchored = rect && rect.width + rect.height > 0
      setCommentComposer({
        anchor: {
          quote: edit.quote,
          quoteAfter: edit.quoteAfter,
          quoteBefore: edit.quoteBefore,
        },
        editingId: edit.id,
        initialText: edit.comment,
        plainText: edit.plainText || edit.quote,
        position: anchored
          ? {
            left: Math.max(
              224,
              Math.min(
                rect.left + rect.width / 2,
                window.innerWidth - 224,
              ),
            ),
            top: rect.bottom + 8,
            transform: 'translate(-50%, 0)',
          }
          : {
            left: window.innerWidth / 2,
            top: 96,
            transform: 'translate(-50%, 0)',
          },
      })
    })
  }
  const openEditForRef = useRef(openEditFor)
  openEditForRef.current = openEditFor

  useEffect(() => {
    const edit = context.canvasCommentEdit
    if (!edit || edit.artifactId !== descriptor.artifactId) return
    if (artifact?.contentMarkdown === undefined) return
    context.clearCanvasCommentEdit()
    openEditForRef.current(edit)
  }, [artifact?.contentMarkdown, context, descriptor.artifactId])

  // Clicking a highlighted (pending) passage opens its comment
  // for editing; the stack pencil stays the second path. A live text selection never triggers it.
  const handleHighlightClick = (event: ReactMouseEvent<HTMLDivElement>) => {
    if (!document.getSelection()?.isCollapsed) return
    const root = markdownRootRef.current
    if (!root) return
    for (const draft of context.canvasComments) {
      if (draft.artifactId !== descriptor.artifactId) continue
      const [range] = findQuoteRanges(root, [draft.plainText || draft.quote])
      if (!range) continue
      const hit = [...range.getClientRects()].some(
        (rect) =>
          event.clientX >= rect.left
          && event.clientX <= rect.right
          && event.clientY >= rect.top
          && event.clientY <= rect.bottom,
      )
      if (hit) {
        openEditForRef.current(draft, range)
        return
      }
    }
  }

  const writing = artifact?.status === 'writing'
  const canEdit = Boolean(
    artifact
    && !writing
    && artifact.contentMarkdown !== undefined
    && canEditAgentRun(run),
  )
  const references = useMemo(
    () => agentArtifactReferences(artifact?.refs),
    [artifact?.refs],
  )

  useEffect(() => setEditing(false), [descriptor.artifactId])

  useEffect(() => {
    if (canEdit) return
    if (saveTimerRef.current !== null) {
      window.clearTimeout(saveTimerRef.current)
      saveTimerRef.current = null
    }
    pendingRef.current = null
    setEditing(false)
  }, [canEdit])

  useEffect(() => {
    if (artifact && artifact.contentMarkdown !== undefined) {
      saveGate.observeServerBody(artifact.contentMarkdown, pendingRef.current !== null)
    }
    if (artifact && artifact.contentMarkdown !== undefined && pendingRef.current === null) {
      baseRevisionRef.current = artifact.revision
    }
  }, [artifact, saveGate])

  const flushSave = useCallback(async () => {
    const markdown = pendingRef.current
    if (markdown === null || !artifact) return
    pendingRef.current = null
    saveGate.submit(markdown)
    let result: Awaited<ReturnType<typeof context.saveArtifact>>
    try {
      result = await context.saveArtifact(
        effectiveRunId,
        descriptor.artifactId,
        markdown,
        baseRevisionRef.current,
      )
    } catch {
      saveGate.settle(markdown, false)
      // A thrown save (network, read-only) must neither lose the text
      // nor stay silent: the entry only clears on a DEFINITIVE outcome.
      if (pendingRef.current === null) {
        pendingRef.current = markdown
      }
      setNotice(t.agent.canvas.saveFailed)
      return
    }
    saveGate.settle(markdown, result.kind === 'saved')
    if (result.kind === 'saved') {
      baseRevisionRef.current = result.revision
      setNotice(null)
    } else if (result.kind === 'locked') {
      setNotice(t.agent.canvas.lockedByAgent)
    } else {
      // Stale edit: the fresh body was reloaded by saveArtifact; say so.
      if (result.currentRevision !== null) {
        baseRevisionRef.current = result.currentRevision
      }
      setNotice(t.agent.canvas.editConflict)
    }
  }, [artifact, context, descriptor.artifactId, effectiveRunId, saveGate, t])

  const handleChange = useCallback(
    (markdown: string) => {
      // Back to the body the server holds (an undo, or an edit reverted
      // while its save is in flight): nothing to save, and a queued save
      // of the intermediate text must not go out either.
      if (saveGate.holds(markdown)) {
        if (saveTimerRef.current !== null) {
          window.clearTimeout(saveTimerRef.current)
          saveTimerRef.current = null
        }
        pendingRef.current = null
        return
      }
      pendingRef.current = markdown
      if (saveTimerRef.current !== null) {
        window.clearTimeout(saveTimerRef.current)
      }
      saveTimerRef.current = window.setTimeout(() => {
        saveTimerRef.current = null
        void flushSave()
      }, CANVAS_SAVE_DEBOUNCE_MS)
    },
    [flushSave, saveGate],
  )

  // Follow-up submits await the pending edit.
  // Registry, not a single slot: during a tab transition the OLD view
  // unmounts AFTER the new one mounted — its cleanup must only remove
  // its OWN registration (the registry compares before deleting).
  useEffect(() => context.saveRegistry.register(
    descriptor.artifactId,
    flushSave,
  ), [context.saveRegistry, descriptor.artifactId, flushSave])

  useEffect(
    () => () => {
      if (saveTimerRef.current !== null) {
        window.clearTimeout(saveTimerRef.current)
      }
      // Pending text can also survive a FAILED flush (restored above) —
      // both cases flush on the way out; flushSave no-ops when clean.
      if (pendingRef.current !== null) {
        void flushSave()
      }
    },
    [flushSave],
  )

  // Edit mode shows the same citations as reading, as chips that open the
  // same evidence view.
  const editCitations = useMemo(() => ({
    isLabel: isAgentCitationLabel,
    labels: new Set(references.map((reference) => reference.label)),
    onOpen: (label: string) => context.openCanvasView({
      artifactId: descriptor.artifactId,
      label,
      runId: effectiveRunId,
      view: 'evidence',
    }),
  }), [context, descriptor.artifactId, effectiveRunId, references])

  const editorDocument = useMemo(() => {
    if (!artifact || artifact.contentMarkdown === undefined) return null
    return {
      contentMarkdown: artifact.contentMarkdown,
      createdAt: new Date(artifact.createdAt * 1000).toISOString(),
      folderId: null,
      // Revision is deliberately NOT part of the identity: the surface
      // resets content on id change only, preserving the cursor.
      id: `agent-artifact-${artifact.artifactId}`,
      revision: artifact.revision,
      source: 'agent-artifact' as const,
      sourceRunId: effectiveRunId,
      title: artifact.title,
      updatedAt: new Date(artifact.updatedAt * 1000).toISOString(),
    }
  }, [artifact, effectiveRunId])

  // The anchor run is the one that delivered this text; its record says
  // how that text's review ended, and marks its statements.
  const outcome = artifact ? documentOutcome(run?.outcome, artifact) : undefined
  const claimMarks = useMemo(
    () => (artifact && !writing ? artifactClaimMarks(outcome, artifact) : []),
    [artifact, outcome, writing],
  )
  const claimNote = useClaimNote()

  if (!artifact) {
    return <CanvasMissing label={t.agent.canvas.empty} />
  }

  const versions = selectableRevisions(artifact.revisions, artifact.revision)
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex shrink-0 items-center gap-2 border-b border-border px-3 py-1.5">
        {writing && (
          <span className="inline-flex items-center gap-1.5 rounded-full border border-brand/20 bg-brand-subtle px-2 py-0.5 t-hint font-semibold text-brand">
            <span
              aria-hidden="true"
              className={cn(
                'size-1.5 rounded-full bg-brand',
                !reduceMotion && 'inqtrix-running-dot',
              )}
            />
            {t.agent.canvas.agentWriting}
          </span>
        )}
        {renaming ? (
          <input
            aria-label={t.agent.canvas.renameDocument}
            autoFocus
            className="h-5 w-48 min-w-0 rounded border border-border bg-card px-1.5 t-hint text-foreground"
            defaultValue={artifact.title}
            onBlur={(event) => void commitRename(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter') {
                void commitRename(event.currentTarget.value)
              }
              if (event.key === 'Escape') setRenaming(false)
            }}
          />
        ) : (
          <button
            aria-label={t.agent.canvas.renameDocument}
            className="group inline-flex min-w-0 items-center gap-1 t-hint font-semibold text-foreground transition-colors hover:text-brand"
            onClick={() => setRenaming(true)}
            title={`${artifact.title} — ${t.agent.canvas.renameDocument}`}
            type="button"
          >
            <span className="truncate">
              {context.sessionFileNames[descriptor.artifactId]
                || artifact.title
                || t.agent.canvas.views.document}
            </span>
            <PenLine
              aria-hidden="true"
              className="size-3 shrink-0 opacity-0 transition-opacity group-hover:opacity-60"
            />
          </button>
        )}
        {versions.length > 0 ? (
          <select
            aria-label={t.agent.plan.versionHistory}
            className="h-5 shrink-0 rounded border border-border bg-card px-1 t-hint tabular-nums text-muted-foreground"
            onChange={(event) => {
              const from = Number(event.target.value)
              if (Number.isFinite(from) && from < artifact.revision) {
                context.openCanvasView({
                  view: 'diff',
                  runId: effectiveRunId,
                  artifactId: descriptor.artifactId,
                  fromRevision: from,
                  toRevision: artifact.revision,
                })
              }
              event.target.value = String(artifact.revision)
            }}
            value={artifact.revision}
          >
            {versions.map((entry) => (
              <option key={entry.revision} value={entry.revision}>
                {t.agent.canvas.revision.replace(
                  '{revision}',
                  String(entry.revision),
                )}
              </option>
            ))}
          </select>
        ) : (
          <span className="t-hint tabular-nums text-muted-foreground">
            {t.agent.canvas.revision.replace('{revision}', String(artifact.revision))}
          </span>
        )}
        {notice && (
          <span className="truncate t-hint font-semibold text-warning" role="alert" title={notice}>{notice}</span>
        )}
        {(exporting || exportedFor === exportSubject) && (
          <span className="truncate t-hint text-muted-foreground" role="status">
            {exporting ? t.agent.canvas.exporting : t.agent.canvas.exported}
          </span>
        )}
        <span className="min-w-0 flex-1" />
        {canEdit && (
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                aria-label={editing ? t.agent.canvas.readMemo : t.agent.canvas.editMemo}
                className="size-7 text-muted-foreground hover:text-foreground"
                onClick={() => {
                  if (!editing) {
                    setEditing(true)
                    return
                  }
                  void flushSave().finally(() => setEditing(false))
                }}
                size="icon"
                type="button"
                variant="ghost"
              >
                {editing ? (
                  <BookOpen className="icon-md" />
                ) : (
                  <PenLine className="icon-md" />
                )}
              </Button>
            </TooltipTrigger>
            <TooltipContent side="bottom">
              {editing ? t.agent.canvas.readMemo : t.agent.canvas.editMemo}
            </TooltipContent>
          </Tooltip>
        )}
        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              aria-label={t.agent.canvas.copyMarkdown}
              className="size-7 text-muted-foreground hover:text-foreground"
              onClick={() => {
                if (artifact.contentMarkdown !== undefined) {
                  void copyTextToClipboard(
                    withAiDisclosure(artifact.contentMarkdown, t.aiTransparency.exportNotice),
                  )
                }
              }}
              size="icon"
              type="button"
              variant="ghost"
            >
              <Copy className="size-4" />
            </Button>
          </TooltipTrigger>
          <TooltipContent side="bottom">{t.agent.canvas.copyMarkdown}</TooltipContent>
        </Tooltip>
        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              aria-label={exporting ? t.agent.canvas.exporting : t.agent.canvas.exportToEditor}
              aria-busy={exporting}
              className="size-7 text-muted-foreground hover:text-foreground"
              disabled={exporting}
              onClick={() => {
                if (exportPendingRef.current) return
                exportPendingRef.current = true
                setExporting(true)
                setExportedFor(null)
                setNotice(null)
                void context
                  .exportArtifact(
                    effectiveRunId,
                    descriptor.artifactId,
                    artifact.title,
                  )
                  .then(() => setExportedFor(exportSubject))
                  .catch((error: unknown) => {
                    console.warn('Canvas export failed.', error)
                    setNotice(t.agent.canvas.exportFailed)
                  })
                  .finally(() => {
                    exportPendingRef.current = false
                    setExporting(false)
                  })
              }}
              size="icon"
              type="button"
              variant="ghost"
            >
              {exporting
                ? <LoaderCircle className="size-4 motion-safe:animate-spin" />
                : exportedFor === exportSubject
                  ? <Check className="size-4 text-success" />
                  : <Download className="size-4" />}
            </Button>
          </TooltipTrigger>
          <TooltipContent side="bottom">{t.agent.canvas.exportToEditor}</TooltipContent>
        </Tooltip>
      </div>
      {commentComposer && (
        <CanvasCommentPopover
          cancelLabel={t.agent.canvas.commentDiscard}
          initialText={commentComposer.initialText}
          key={commentComposer.editingId ?? 'new'}
          onCancel={() => setCommentComposer(null)}
          onSubmit={(text) =>
            queueComment(
              commentComposer.anchor,
              text,
              commentComposer.plainText,
              commentComposer.editingId,
            )}
          placeholder={t.agent.canvas.commentPlaceholder}
          position={commentComposer.position}
          quotePreview={commentComposer.plainText
            || commentComposer.anchor.quote}
          submitLabel={t.agent.canvas.commentQueue}
        />
      )}
      <ScrollArea className="min-h-0 flex-1">
        <div className={canvasSurfaceClass}>
          {editing && canEdit && editorDocument ? (
            <MarkdownEditorSurface
              citations={editCitations}
              comments={[]}
              copy={editorCopy[locale]}
              diffAnchorMarkdown={null}
              document={editorDocument}
              embedded
              isDiffVisible={false}
              mode="live"
              onAcceptSuggestion={noop}
              onChange={handleChange}
              onCreateComment={(record) => {
                // The bubble menu collected the note; only the anchor
                // shape is translated (selectedMarkdown wins — it is
                // the source-level quote the server relays verbatim).
                queueComment(
                  {
                    quote:
                      record.anchor.selectedMarkdown
                      || record.anchor.selectedText,
                    quoteAfter: record.anchor.quoteAfter,
                    quoteBefore: record.anchor.quoteBefore,
                  },
                  record.commentMarkdown,
                  record.anchor.selectedText,
                )
              }}
              // Edit mode hands the comment to the SAME compact
              // popover as read mode — one comment field per canvas
              // (the built-in editor form with its kind chips stays an
              // editor-only surface).
              externalCommentComposer={({ anchor, position }) => {
                setCommentComposer({
                  anchor: {
                    quote:
                      anchor.selectedMarkdown || anchor.selectedText,
                    quoteAfter: anchor.quoteAfter,
                    quoteBefore: anchor.quoteBefore,
                  },
                  editingId: null,
                  initialText: '',
                  plainText: anchor.selectedText,
                  position: {
                    left: Math.max(
                      224,
                      Math.min(position.left, window.innerWidth - 224),
                    ),
                    top: position.top,
                    transform: 'translate(-50%, 0)',
                  },
                })
              }}
              onEditSuggestion={noop}
              onEditorReady={noop}
              onMarkSuggestionStale={noop}
              onRefineSuggestion={asyncNoop}
              onRejectSuggestion={noop}
              onSelectComment={noop}
              onStopSuggestion={noop}
              runningSuggestionIds={[]}
              selectedCommentId={null}
              suggestionErrors={{}}
              suggestions={[]}
              textImprovement={{
                enabled: false,
                workspaceId: context.workspaceId,
              }}
            />
          ) : artifact.contentMarkdown !== undefined ? (
            <div
              className="min-w-0"
              onClick={handleHighlightClick}
              ref={markdownRootRef}
            >
            <MarkdownSelectionCopyMenu
              action={{
                label: t.agent.canvas.commentAction,
                onSelect: (selection, menuContext) => {
                  const quote = selection.markdownText ?? selection.plainText
                  setCommentComposer({
                    anchor: anchorFromMarkdownQuote(
                      artifact.contentMarkdown ?? '',
                      quote,
                    ),
                    editingId: null,
                    initialText: '',
                    plainText: selection.plainText,
                    position: menuContext.position,
                  })
                },
              }}
              aiGenerated
              className="w-full min-w-0 max-w-full text-sm leading-snug [overflow-wrap:anywhere]"
              markdown={artifact.contentMarkdown}
              onBlurCapture={claimNote.onBlurCapture}
              onClickCapture={(event: MouseEvent<HTMLDivElement>) => {
                claimNote.onClickCapture(event)
                const anchor = (event.target as HTMLElement | null)?.closest('a')
                const label = agentCitationLabelFromHref(
                  anchor?.getAttribute('href'),
                )
                if (!label || !references.some((item) => item.label === label)) {
                  return
                }
                event.preventDefault()
                event.stopPropagation()
                context.openCanvasView({
                  artifactId: descriptor.artifactId,
                  label,
                  runId: effectiveRunId,
                  view: 'evidence',
                })
              }}
              onFocusCapture={claimNote.onFocusCapture}
            >
              <MarkdownRenderer
                claimMarks={claimMarks}
                isStreaming={writing}
                markdown={linkifyAgentArtifactCitations(
                  artifact.contentMarkdown,
                  references,
                )}
                variant="chat"
              />
            </MarkdownSelectionCopyMenu>
            {claimMarks.length > 0 && (
              <ClaimNote
                anchor={claimNote.anchor}
                line={claimNote.line}
                note={claimNote.note}
                onClose={claimNote.close}
              />
            )}
            </div>
          ) : (
            <p className="t-hint text-muted-foreground">…</p>
          )}
          {outcome && (
            <RunOutcomeLine className="mt-4" outcome={outcome} t={t} />
          )}
          {references.length > 0 && (
            <AgentArtifactSources
              onOpen={(label) => context.openCanvasView({
                artifactId: descriptor.artifactId,
                label,
                runId: effectiveRunId,
                view: 'evidence',
              })}
              references={references}
            />
          )}
        </div>
      </ScrollArea>
    </div>
  )
}

/** Artifact evidence rendered with the established Knowledge source-row
 * hierarchy. Inline chips are intentionally absent; sources live where a
 * reader expects them: after the report body. */
function AgentArtifactSources({
  onOpen,
  references,
}: {
  onOpen: (label: string) => void
  references: AgentArtifactReference[]
}) {
  const { t } = useLocale()
  const views = citationViews(
    references.map(agentReferenceAsKnowledge),
    [],
    t.knowledge.viewerSection,
  ).map((view) => ({ ...view, canOpen: true }))
  const internalGroups = new Map(
    groupCitationsByDocument(
      views.filter((view) => view.documentId !== null),
    ).map((group) => [group.documentId, group]),
  )
  const groups = taskResultReferenceGroups(
    references.map((reference) => ({
      chunk_index: reference.chunkIndex,
      document_id: reference.documentId,
      excerpt: reference.excerpt,
      grounded_support: reference.groundedSupport,
      label: reference.label,
      page_number: reference.pageNumber,
      citation_id: reference.citationId,
      provider_snippet: reference.providerSnippet,
      query_id: reference.queryId,
      source_id: reference.sourceId,
      title: reference.title,
      url: reference.url,
    })),
  )
  return (
    <section className="mt-8 border-t border-border/70 pt-4">
      <h2 className="t-section text-foreground">{t.knowledge.sources}</h2>
      <ul className="mt-2 space-y-2">
        {groups.flatMap((sourceGroup) => {
          if (sourceGroup.kind === 'web') {
            return [
              <li key={sourceGroup.reference.key}>
                <WebEvidenceSourceRow
                  onInspect={() => {
                    if (sourceGroup.reference.label) {
                      onOpen(sourceGroup.reference.label)
                    }
                  }}
                  reference={sourceGroup.reference}
                />
              </li>,
            ]
          }
          const documentId = sourceGroup.references[0]?.documentId
          const group = documentId ? internalGroups.get(documentId) : undefined
          if (!group) return []
          if (group.citations.length === 1) {
            const view = group.citations[0]
            return view ? [
              <li key={`${documentId}:${view.label}`}>
                <CitationRow
                  onOpen={(selected) => onOpen(selected.label)}
                  view={view}
                />
              </li>,
            ] : []
          }
          return [
            <li key={documentId}>
              <CitationGroupList
                activeKey={null}
                groups={[group]}
                onOpen={(selected) => onOpen(selected.label)}
                onOpenDocument={(selectedGroup) => {
                  const selected = firstOpenableCitation(selectedGroup)
                  if (selected) onOpen(selected.label)
                }}
              />
            </li>,
          ]
        })}
      </ul>
    </section>
  )
}

// --- plan ---------------------------------------------------------------------

export function PlanCanvasView({
  descriptor,
}: {
  descriptor: Extract<CanvasViewDescriptor, { view: 'plan' }>
}) {
  const context = useAgentCanvas()
  const { t } = useLocale()
  const run = context.runs[descriptor.runId]
  const planApproval = usePlanApproval({
    decideApproval: context.decideApproval,
    draft: context.planDrafts[descriptor.runId] ?? null,
    onDraftChange: (draft) => context.setPlanDraft(descriptor.runId, draft),
    run: run ?? emptyRunPlaceholder,
  })
  // On-open refresh: the background loop only fetches on a stale flag, and
  // a pre-plan 404 clears that flag with no content — opening the tab in
  // either window would render empty forever. One re-flag per open (the
  // ref guard keeps the 404 -> clear -> re-flag cycle from looping); a
  // later `plan.proposed` event re-flags through the normal path.
  const planRequestedForRef = useRef<string | null>(null)
  const hasRun = Boolean(run)
  const hasPlan = Boolean(run?.plan)
  const planStale = Boolean(run?.planStale)
  useEffect(() => {
    if (!hasRun || hasPlan || planStale) return
    if (planRequestedForRef.current === descriptor.runId) return
    planRequestedForRef.current = descriptor.runId
    context.requestPlanRefresh(descriptor.runId)
  }, [context, descriptor.runId, hasPlan, hasRun, planStale])
  if (!run) return <CanvasMissing label={t.agent.plan.noPlan} />
  const pending = planApproval.pendingApproval
  const rejecting = planApproval.draft?.rejectPending ?? false
  const rejectNote = planApproval.draft?.rejectNote ?? ''
  // ONE write per intent: two updateDraft calls in the same tick both
  // start from the same base, so the second silently reverts the first
  // (cancel left the note open, which the live check caught).
  const setRejecting = (value: boolean) =>
    planApproval.updateDraft((draft) => ({
      ...draft,
      rejectNote: value ? draft.rejectNote : '',
      rejectPending: value,
    }))
  const setRejectNote = (value: string) =>
    planApproval.updateDraft((draft) => ({ ...draft, rejectNote: value }))
  const canEdit = canEditAgentRun(run)
  return (
    <ScrollArea className="min-h-0 flex-1">
      <div className={cn(canvasSurfaceClass, 'space-y-3')}>
        <PlanReviewBody
          decidedGuidance={decidedReportGuidance(run.approvals)}
          decidedRuleLabels={decidedReportRuleLabels(run.approvals)}
          reportGuidanceMaxChars={context.reportGuidanceMaxChars}
          reportRuleIdsMax={context.reportRuleIdsMax}
          // A rule the run is frozen under stays offered and keepable
          // even after the library edited, unshared or deleted it.
          reportRuleOptions={gateRuleOptions(context.reportRuleOptions, planApproval.inForce)}
          density="full"
          draft={planApproval.draft}
          editable={Boolean(pending) && canEdit}
          planSource={context.planSource}
          run={run}
          updateDraft={planApproval.updateDraft}
        />
        {run.plan && run.plan.versions.length > 1 && (
          <div>
            <p className="t-caption text-muted-foreground">
              {t.agent.plan.versionHistory}
            </p>
            <ul className="mt-1 space-y-0.5">
              {run.plan.versions.map((version) => (
                <li
                  className="t-meta tabular-nums text-muted-foreground"
                  key={version.planId}
                >
                  v{version.version} · {version.status} · {version.createdBy}
                  {version.reason ? ` · ${version.reason}` : ''}
                </li>
              ))}
            </ul>
          </div>
        )}
        {planApproval.error && (
          <p className="t-meta-sm text-destructive">{planApproval.error}</p>
        )}
        {/* Rejecting with a note: the gate's own hint asks for one and
            the server has always accepted it, but no surface ever sent
            one — the note went into the rejection receipt the agent
            reads, so asking for it and dropping it was the worst of
            both. One step: reject opens the field, sending closes it. */}
        {pending && canEdit && rejecting && (
          <div className="space-y-1.5">
            <label
              className="t-hint text-muted-foreground/80"
              htmlFor="agent-plan-reject-note"
            >
              {t.agent.plan.rejectNote}
            </label>
            <textarea
              autoFocus
              className="w-full resize-none rounded-md border border-border bg-card px-2.5 py-1.5 t-meta text-foreground placeholder:text-muted-foreground/70 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
              id="agent-plan-reject-note"
              maxLength={2000}
              onChange={(event) => setRejectNote(event.target.value)}
              placeholder={t.agent.plan.rejectNotePlaceholder}
              rows={2}
              value={rejectNote}
            />
          </div>
        )}
        {pending && canEdit && (
          <div className="flex items-center justify-end gap-1.5">
            {rejecting && (
              <Button
                className="h-7 px-2.5 text-xs"
                disabled={planApproval.submitting}
                onClick={() =>
                  planApproval.updateDraft((draft) => ({
                    ...draft,
                    rejectNote: '',
                    rejectPending: false,
                  }))}
                size="sm"
                type="button"
                variant="ghost"
              >
                {t.agent.plan.rejectCancel}
              </Button>
            )}
            <Button
              className="h-7 px-2.5 text-xs"
              disabled={planApproval.submitting}
              onClick={() => {
                if (!rejecting) {
                  setRejecting(true)
                  return
                }
                void planApproval.decide('reject', rejectNote.trim())
              }}
              size="sm"
              type="button"
              variant="outline"
            >
              {rejecting
                ? t.agent.plan.rejectSend
                : t.agent.timeline.reject}
            </Button>
            {!rejecting && (
              <Button
                className="h-7 bg-brand px-2.5 text-xs text-brand-foreground hover:bg-brand/90"
                disabled={planApproval.submitting}
                onClick={() => void planApproval.decide('approve')}
                size="sm"
                type="button"
              >
                {t.agent.timeline.approve}
              </Button>
            )}
          </div>
        )}
      </div>
    </ScrollArea>
  )
}

// --- evidence -------------------------------------------------------------------

export function EvidenceCanvasView({
  descriptor,
}: {
  descriptor: Extract<CanvasViewDescriptor, { view: 'evidence' }>
}) {
  const context = useAgentCanvas()
  const { t } = useLocale()
  const run = context.runs[descriptor.runId]
  const artifact = run?.artifacts[descriptor.artifactId]
  const references = agentArtifactReferences(artifact?.refs)
  const reference = references.find((item) => item.label === descriptor.label)
  const evidenceArtifact = Object.values(run?.artifacts ?? {}).find(
    (item) => item.kind === 'evidence_bundle',
  )
  const lineage = reference
    ? evidenceLineageFromArtifactPayload(evidenceArtifact?.payload, reference)
    : null
  const safeReferenceUrl = safeEvidenceHttpUrl(reference?.url ?? null)
  const [chunk, setChunk] = useState<KnowledgeChunkDetail | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [lineageLoadFailed, setLineageLoadFailed] = useState(false)
  // The document reader is opt-in per citation — the exact
  // chunk stays the default view, the full document is one click away.
  const [readerOpen, setReaderOpen] = useState(false)
  const reduceMotion = Boolean(useReducedMotion())
  // The push marks the covered page `inert`, which evicts keyboard
  // focus to <body>; land it on Back instead (run-detail precedent).
  const readerBackRef = useRef<HTMLButtonElement | null>(null)
  useEffect(() => {
    if (readerOpen) readerBackRef.current?.focus({ preventScroll: true })
  }, [readerOpen])

  const loadEvidenceArtifact = context.loadArtifact
  const evidenceBodyMissing = Boolean(artifact) && artifact?.contentMarkdown === undefined
  useEffect(() => {
    if (!evidenceBodyMissing) return
    void loadEvidenceArtifact(descriptor.runId, descriptor.artifactId)
  }, [descriptor.artifactId, descriptor.runId, evidenceBodyMissing, loadEvidenceArtifact])

  useEffect(() => {
    setLineageLoadFailed(false)
    if (
      reference?.documentId
      || (!reference?.queryId && !reference?.sourceId)
      || !evidenceArtifact
      || evidenceArtifact.payload !== undefined
    ) return
    void loadEvidenceArtifact(descriptor.runId, evidenceArtifact.artifactId)
      .catch(() => setLineageLoadFailed(true))
  }, [
    loadEvidenceArtifact,
    descriptor.runId,
    evidenceArtifact,
    reference?.documentId,
    reference?.queryId,
    reference?.sourceId,
  ])

  useEffect(() => {
    setChunk(null)
    setError(null)
    setReaderOpen(false)
    if (!reference?.documentId || !context.clientOptions) return
    let cancelled = false
    getKnowledgeChunk(
      reference.documentId,
      reference.chunkIndex ?? 0,
      1,
      context.clientOptions,
    )
      .then((detail) => {
        if (!cancelled) setChunk(detail)
      })
      .catch((caught: unknown) => {
        if (!cancelled) {
          setError(caught instanceof Error ? caught.message : String(caught))
        }
      })
    return () => {
      cancelled = true
    }
  }, [context.clientOptions, reference?.chunkIndex, reference?.documentId])

  const collectionLabel = context.planSource.collections.find(
    (entry) => entry.id === reference?.collectionId,
  )?.title
  // Stable identity: the reader keys its highlight pass on this object,
  // so a fresh literal per render would re-run the whole match pass.
  const readerTarget = useMemo(
    () =>
      reference
        ? agentReferenceViewerTarget(
          reference,
          chunk?.excerpt ?? null,
          collectionLabel,
        )
        : null,
    [chunk?.excerpt, collectionLabel, reference],
  )

  if (!artifact) {
    return <CanvasMissing label={t.agent.canvas.evidenceUnavailable} />
  }
  if (artifact.contentMarkdown === undefined) {
    return <CanvasMissing label="…" />
  }
  if (!reference) {
    return <CanvasMissing label={t.agent.canvas.evidenceUnavailable} />
  }
  // Page-in-page push (the shared list-to-detail motion, same shape as the
  // run detail): the document reader takes the FULL surface instead of a
  // vertical split — a citation reader needs the width, and the canvas
  // panel is often narrow. The evidence layer stays mounted (scroll
  // position survives) and `inert` blocks the covered page.
  return (
    <div className="relative flex min-h-0 flex-1 overflow-hidden">
      <motion.div
        animate={
          reduceMotion
            ? { opacity: readerOpen ? 0 : 1, x: 0 }
            : { x: readerOpen ? '-30%' : '0%' }
        }
        className="absolute inset-0 flex min-h-0 flex-col"
        inert={readerOpen || undefined}
        transition={
          reduceMotion
            ? { duration: 0.12 }
            : readerOpen
              ? appMotion.push
              : appMotion.pushExit
        }
      >
    <ScrollArea className="min-h-0 flex-1">
      <div className={cn(canvasSurfaceClass, 'space-y-4')}>
        <div className="flex min-w-0 items-start gap-2">
          {reference.documentId ? (
            <BookOpen className="mt-0.5 icon-md shrink-0 text-brand" />
          ) : (
            <Globe2 className="mt-0.5 icon-md shrink-0 text-brand" />
          )}
          <div className="min-w-0 flex-1">
            <p className="break-words t-section text-foreground">
              {reference.title}
            </p>
            <p className="mt-0.5 t-meta-sm text-muted-foreground">
              {reference.label} · {reference.documentId
                ? t.agent.canvas.knowledgeEvidence
                : t.agent.canvas.webEvidence}
            </p>
          </div>
          {readerTarget && (
            <Button
              className="shrink-0 gap-1.5 px-2 text-muted-foreground hover:text-foreground"
              onClick={() => setReaderOpen(true)}
              size="sm"
              type="button"
              variant="ghost"
            >
              <BookOpen className="icon-sm" />
              {t.agent.canvas.showInDocument}
            </Button>
          )}
        </div>
        {/* The raw URL belongs to WEB evidence. A knowledge citation's
            url is the internal source endpoint — showing it offered a
            dead-end API link where the document reader belongs. */}
        {safeReferenceUrl && !reference.documentId && (
          <a
            className="inline-flex max-w-full items-center gap-1.5 t-meta text-brand hover:underline"
            href={safeReferenceUrl}
            rel="noreferrer noopener"
            target="_blank"
          >
            <ExternalLink className="icon-sm shrink-0" />
            <span className="truncate">{reference.url}</span>
          </a>
        )}
        {reference.groundedSupport && !lineage && (
          <section className="rounded-lg border border-brand/20 bg-brand-subtle/25 px-3 py-3">
            <p className="t-caption uppercase tracking-wide text-muted-foreground">
              {t.agent.canvas.providerGroundedSupport}
            </p>
            <p className="mt-1 whitespace-pre-wrap break-words t-body text-foreground/90">
              {reference.groundedSupport}
            </p>
          </section>
        )}
        {!reference.documentId
          && !reference.excerpt
          && !reference.groundedSupport
          && !lineage && (
          <p className="t-meta text-muted-foreground">
            {t.agent.canvas.legacyEvidenceUnavailable}
          </p>
        )}
        {reference.documentId && (
          <p className="t-mono text-muted-foreground">
            doc:{reference.documentId}#{reference.chunkIndex ?? 0}
          </p>
        )}
        {error && <p className="t-meta text-destructive">{error}</p>}
        {reference.documentId && chunk && (
          <>
            <p className="t-caption uppercase tracking-wide text-muted-foreground">
              {t.agent.canvas.exactKnowledgeChunk}
            </p>
            <blockquote className="rounded-md border-l-2 border-brand bg-brand-subtle/30 px-3 py-2 t-body text-foreground">
              {chunk.excerpt}
            </blockquote>
            {chunk.page_number !== null && (
              <p className="t-hint text-muted-foreground">
                S. {chunk.page_number}
              </p>
            )}
            {/* The surrounding passages are DOCUMENT CONTEXT, not part of
                the citation. Unlabeled they read as if the agent had
                cited them too — and the passage before a chunk often
                belongs to a different article entirely. */}
            {(chunk.neighbors?.length ?? 0) > 0 && (
              <section className="space-y-1.5 rounded-md border border-border/60 bg-surface/40 px-3 py-2">
                <p className="t-caption uppercase tracking-wide text-muted-foreground">
                  {t.agent.canvas.surroundingContext}
                </p>
                {chunk.neighbors?.map((neighbor) => (
                  <div key={neighbor.chunk_index}>
                    <p className="t-hint text-muted-foreground/80">
                      {(neighbor.chunk_index < chunk.chunk_index
                        ? t.agent.canvas.contextBefore
                        : t.agent.canvas.contextAfter
                      ).replace('{n}', String(neighbor.chunk_index + 1))}
                    </p>
                    <p className="mt-0.5 t-meta text-muted-foreground">
                      {neighbor.excerpt}
                    </p>
                  </div>
                ))}
              </section>
            )}
          </>
        )}
        {reference.documentId && !chunk && !error && (
          <p className="t-hint text-muted-foreground">…</p>
        )}
        <EvidenceProvenancePanel
          lineage={lineage}
          lineageLoadFailed={lineageLoadFailed}
          reference={reference}
        />
      </div>
    </ScrollArea>
      </motion.div>
      <AnimatePresence initial={false}>
        {readerTarget && readerOpen && (
          <motion.div
            animate={reduceMotion ? { opacity: 1, x: 0 } : { x: '0%' }}
            className="absolute inset-0 z-10 flex min-h-0 flex-col bg-background shadow-[-12px_0_24px_-12px_var(--shadow-soft)]"
            exit={
              reduceMotion
                ? { opacity: 0, x: 0, transition: { duration: 0.12 } }
                : { x: '100%', transition: appMotion.pushExit }
            }
            initial={reduceMotion ? { opacity: 0, x: 0 } : { x: '100%' }}
            key={readerTarget.documentId}
            transition={reduceMotion ? { duration: 0.12 } : appMotion.push}
          >
            <div className="flex shrink-0 items-center gap-2 border-b border-border px-3 py-2">
              <Button
                className="gap-1.5 px-2 text-muted-foreground hover:text-foreground"
                onClick={() => setReaderOpen(false)}
                ref={readerBackRef}
                size="sm"
                type="button"
                variant="ghost"
              >
                <ChevronLeft className="icon-sm" />
                {t.agent.canvas.backToEvidence}
              </Button>
              <p className="min-w-0 flex-1 truncate t-meta-sm text-muted-foreground">
                {reference.label} · {reference.title}
              </p>
            </div>
            <DocumentReader
              collectionLabel={readerTarget.collectionLabel}
              dataSource={context.knowledgeDataSource}
              target={readerTarget}
            />
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}

// --- file -----------------------------------------------------------------------

export function FileCanvasView({
  descriptor,
}: {
  descriptor: Extract<CanvasViewDescriptor, { view: 'file' }>
}) {
  const context = useAgentCanvas()
  const { t } = useLocale()
  const { originalOpened, switchTab, tab } = useFilePreviewTabs()
  const asset: FileAssetRecord | undefined = descriptor.assetId
    ? context.fileAssets[descriptor.assetId]
    : undefined
  if (!asset) {
    return <CanvasMissing label={t.agent.canvas.views.file} />
  }
  const canShowOriginal = Boolean(asset.serverFileId && context.clientOptions)
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex shrink-0 items-center gap-2 border-b border-border px-3 py-1.5">
        <FileText className="icon-sm shrink-0 text-muted-foreground" />
        <span className="min-w-0 flex-1 truncate t-meta text-muted-foreground">
          {asset.fileName}
        </span>
        <FilePreviewTabSwitch
          canShowOriginal={canShowOriginal}
          onSwitch={switchTab}
          tab={tab}
        />
      </div>
      <FilePreviewBody
        asset={asset}
        options={context.clientOptions}
        originalOpened={originalOpened}
        tab={tab}
      />
    </div>
  )
}

// --- diff -----------------------------------------------------------------------

export function DiffCanvasView({
  descriptor,
}: {
  descriptor: Extract<CanvasViewDescriptor, { view: 'diff' }>
}) {
  const context = useAgentCanvas()
  const { locale, t } = useLocale()
  const [fromMarkdown, setFromMarkdown] = useState<string | null>(null)
  const [toMarkdown, setToMarkdown] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  // Anchor-independent resolution: the descriptor's runId can be a
  // stale anchor after a later update re-anchored the artifact — the
  // raw id would 404 where DocumentCanvasView resolves correctly.
  const effectiveRunId = resolveAgentArtifact(
    context.runs,
    context.sessionArtifacts,
    descriptor,
  ).runId

  // Pinned revisions never change: the diff loads once per revision pair,
  // not on every event of a running run (the context changes with each).
  const loadRevision = context.loadArtifact
  const { artifactId: diffArtifactId, fromRevision, toRevision } = descriptor
  useEffect(() => {
    let cancelled = false
    setFromMarkdown(null)
    setToMarkdown(null)
    setError(null)
    void Promise.all([
      loadRevision(effectiveRunId, diffArtifactId, fromRevision),
      loadRevision(effectiveRunId, diffArtifactId, toRevision),
    ])
      .then(([from, to]) => {
        if (cancelled) return
        setFromMarkdown(from.content_markdown)
        setToMarkdown(to.content_markdown)
      })
      .catch((caught: unknown) => {
        if (!cancelled) {
          setError(caught instanceof Error ? caught.message : String(caught))
        }
      })
    return () => {
      cancelled = true
    }
  }, [diffArtifactId, effectiveRunId, fromRevision, loadRevision, toRevision])

  if (error) {
    return <CanvasMissing label={error} />
  }
  if (fromMarkdown === null || toMarkdown === null) {
    return <CanvasMissing label="…" />
  }
  return (
    <ScrollArea className="min-h-0 flex-1">
      <div className={canvasSurfaceClass}>
        <p className="mb-3 t-hint tabular-nums text-muted-foreground">
          {t.agent.canvas.revision.replace('{revision}', String(descriptor.fromRevision))}
          {' → '}
          {t.agent.canvas.revision.replace('{revision}', String(descriptor.toRevision))}
        </p>
        <DocumentDiffView
          anchorMarkdown={fromMarkdown}
          copy={editorCopy[locale]}
          currentMarkdown={toMarkdown}
        />
      </div>
    </ScrollArea>
  )
}

// --- patch ----------------------------------------------------------------------

/**
 * Patch review: the agent's proposed edits against the target editor
 * document, one diff block per edit (the suggestion-diff vocabulary), with
 * the always-gated approve&apply / reject actions shared with the timeline
 * card via `usePatchReview`.
 */
export function PatchCanvasView({
  descriptor,
}: {
  descriptor: Extract<CanvasViewDescriptor, { view: 'patch' }>
}) {
  const context = useAgentCanvas()
  const { t } = useLocale()
  const run = context.runs[descriptor.runId]
  const review = usePatchReview({
    actions: {
      applyPatch: context.applyPatch,
      decideApproval: context.decideApproval,
      rejectPatch: context.rejectPatch,
    },
    run: run ?? emptyRunPlaceholder,
  })
  if (!run) return <CanvasMissing label={t.agent.canvas.views.patch} />
  const patch = run.patch
  if (!patch) return <CanvasMissing label="…" />
  const gatesActive = isGateAgentRun(run.status)
  const decided = patch.status !== 'pending'
  const canEdit = canEditAgentRun(run)

  return (
    <ScrollArea className="min-h-0 flex-1">
      <div className={cn(canvasSurfaceClass, 'space-y-3')}>
        {patch.summary && (
          <p className="t-body text-foreground/90">{patch.summary}</p>
        )}
        <p className="t-hint tabular-nums text-muted-foreground">
          {countLabel(
            patch.edits.length,
            t.agent.patch.editCountOne,
            t.agent.patch.editCount,
          )}
          {decided && (
            <span className="ml-2 font-semibold">
              {patch.status === 'accepted'
                ? t.agent.patch.applied
                : t.agent.patch.rejected}
            </span>
          )}
        </p>
        {patch.warnings.map((warning, index) => (
          <p className="t-meta-sm text-warning" key={index}>
            {warning}
          </p>
        ))}
        <ul className="space-y-2">
          {patch.edits.map((edit) => (
            <PatchEditBlock
              edit={edit}
              key={edit.id}
              skipped={
                patch.appliedEditIds !== null
                && !patch.appliedEditIds.includes(edit.id)
              }
              t={t}
            />
          ))}
        </ul>
        {review.notice && (
          <p className="t-meta text-warning">{review.notice}</p>
        )}
        {review.pendingApproval && gatesActive && !decided && canEdit && (
          <div className="flex items-center justify-end gap-1.5">
            <Button
              className="h-7 px-2.5 text-xs"
              disabled={review.submitting}
              onClick={() => void review.reject()}
              size="sm"
              type="button"
              variant="outline"
            >
              {t.agent.timeline.reject}
            </Button>
            <Button
              className="h-7 bg-brand px-2.5 text-xs text-brand-foreground hover:bg-brand/90"
              disabled={review.submitting}
              onClick={() => void review.approveAndApply()}
              size="sm"
              type="button"
            >
              {t.agent.patch.applyAndApprove}
            </Button>
          </div>
        )}
      </div>
    </ScrollArea>
  )
}

function PatchEditBlock({
  edit,
  skipped,
  t,
}: {
  edit: { id: string; find: string; position: string; text: string; note: string }
  skipped: boolean
  t: ReturnType<typeof useLocale>['t']
}) {
  const isReplace = edit.position === 'replace' && edit.find
  const isDelete = isReplace && !edit.text.trim()
  const segments = isReplace && !isDelete
    ? suggestionDiffSegments(edit.find, edit.text)
    : null
  return (
    <li
      className={cn(
        'rounded-md border border-border bg-surface/50 px-3 py-2',
        skipped && 'opacity-60',
      )}
    >
      {edit.note && (
        <p className="mb-1.5 t-meta text-muted-foreground">{edit.note}</p>
      )}
      {segments ? (
        <p className="whitespace-pre-wrap t-body">
          {segments.map((segment, index) =>
            segment.type === 'equal' ? (
              <span key={index}>{segment.text}</span>
            ) : segment.type === 'delete' ? (
              <del className="rounded-sm bg-destructive/10 text-destructive/90 line-through decoration-destructive/50" key={index}>
                {segment.text}
              </del>
            ) : (
              <ins className="rounded-sm bg-success-subtle text-success no-underline" key={index}>
                {segment.text}
              </ins>
            ),
          )}
        </p>
      ) : isDelete ? (
        <p className="whitespace-pre-wrap t-body">
          <span className="t-caption text-destructive">{t.agent.patch.deletion}</span>
          <del className="mt-1 block rounded-sm bg-destructive/10 text-destructive/90 line-through decoration-destructive/50">
            {edit.find}
          </del>
        </p>
      ) : (
        <div className="t-body">
          <span className="t-caption text-success">{t.agent.patch.insertion}</span>
          {edit.find && (
            <p className="mt-1 whitespace-pre-wrap t-meta text-muted-foreground">
              {edit.find}
            </p>
          )}
          <ins className="mt-1 block whitespace-pre-wrap rounded-sm bg-success-subtle text-success no-underline">
            {edit.text}
          </ins>
        </div>
      )}
    </li>
  )
}

// --- shared -------------------------------------------------------------------

// --- run (Verlauf) -------------------------------------------------------------

/** The live run view is the Agent Desk control room. A selected task remains
 * inside this run tab, so users drill down and Back without creating a second
 * canvas document or losing the run overview. */
export function RunCanvasView({
  descriptor,
}: {
  descriptor: Extract<CanvasViewDescriptor, { view: 'run' }>
}) {
  const context = useAgentCanvas()
  const { t } = useLocale()
  const reduceMotion = Boolean(useReducedMotion())
  const restoredFocusRef = useRef(false)
  const run = context.runs[descriptor.runId]
  const inspectedChild = !run || run.kind === 'agent_child'
  const [inspectionError, setInspectionError] = useState<string | null>(null)
  const [inspectionTransport, setInspectionTransport] = useState('connecting')
  const [inspectionAttempt, setInspectionAttempt] = useState(0)
  useEffect(() => {
    if (!inspectedChild) return undefined
    const controller = new AbortController()
    setInspectionError(null)
    setInspectionTransport('connecting')
    if (!context.clientOptions) {
      setInspectionError(t.agent.canvas.child.connectionUnavailable)
      return undefined
    }
    void context.inspectChildRun(descriptor.runId, controller.signal, setInspectionTransport)
      .then(() => { if (!controller.signal.aborted) setInspectionTransport('settled') })
      .catch((error: unknown) => {
        if (!controller.signal.aborted) setInspectionError(error instanceof Error ? error.message : t.agent.canvas.child.runUnavailable)
      })
    return () => controller.abort()
  }, [
    context.clientOptions,
    context.inspectChildRun,
    descriptor.runId,
    inspectedChild,
    inspectionAttempt,
    t.agent.canvas.child.connectionUnavailable,
    t.agent.canvas.child.runUnavailable,
  ])
  if (inspectedChild && inspectionError) {
    return (
      <div className="p-4">
        <p className="t-meta text-warning" role="alert">{inspectionError}</p>
        <Button className="mt-3" onClick={() => setInspectionAttempt((value) => value + 1)} variant="outline">
          {t.agent.canvas.child.retry}
        </Button>
      </div>
    )
  }
  if (!run) return <CanvasMissing label={t.agent.canvas.child.connectionConnecting} />
  const selectedTaskId = descriptor.taskId
  const detailOpen = Boolean(selectedTaskId)
  // Page-in-page push (the shared list-to-detail motion): the detail layer slides
  // in full width while the list parallaxes to -30% and stays MOUNTED
  // (scroll position and focus survive; `inert` blocks the covered
  // layer). Percent-based transforms keep the push resize-proof inside
  // the resizable panel; reduced motion collapses to a 120ms fade.
  return (
    <div className="flex min-h-0 flex-1 flex-col">
    {inspectedChild && (inspectionTransport === 'connecting' || inspectionTransport === 'polling') && (
      <p className="border-b border-border px-3 py-2 t-hint text-muted-foreground" role="status">
        {inspectionTransport === 'polling' ? t.agent.canvas.child.connectionPolling : t.agent.canvas.child.connectionConnecting}
      </p>
    )}
    <div className="relative flex min-h-0 flex-1 overflow-hidden">
      <motion.div
        animate={
          reduceMotion
            ? { opacity: detailOpen ? 0 : 1, x: 0 }
            : { x: detailOpen ? '-30%' : '0%' }
        }
        className="absolute inset-0 flex min-h-0 flex-col"
        inert={detailOpen || undefined}
        transition={
          reduceMotion
            ? { duration: 0.12 }
            : detailOpen
              ? appMotion.push
              : appMotion.pushExit
        }
      >
        <RunOverview
          descriptor={descriptor}
          restoredFocusRef={restoredFocusRef}
          run={run}
        />
      </motion.div>
      <AnimatePresence initial={false}>
        {selectedTaskId && (
          <motion.div
            animate={reduceMotion ? { opacity: 1, x: 0 } : { x: '0%' }}
            className="absolute inset-0 z-10 flex min-h-0 flex-col bg-background shadow-[-12px_0_24px_-12px_var(--shadow-soft)]"
            exit={
              reduceMotion
                ? { opacity: 0, x: 0, transition: { duration: 0.12 } }
                : { x: '100%', transition: appMotion.pushExit }
            }
            initial={reduceMotion ? { opacity: 0, x: 0 } : { x: '100%' }}
            key={selectedTaskId}
            transition={reduceMotion ? { duration: 0.12 } : appMotion.push}
          >
            <TaskDetailView
              onBack={() => {
                context.openCanvasView({
                  focusTaskId: selectedTaskId,
                  runId: descriptor.runId,
                  view: 'run',
                })
              }}
              run={run}
              taskId={selectedTaskId}
            />
          </motion.div>
        )}
      </AnimatePresence>
    </div>
    </div>
  )
}

function RunOverview({
  descriptor,
  restoredFocusRef,
  run,
}: {
  descriptor: Extract<CanvasViewDescriptor, { view: 'run' }>
  restoredFocusRef: MutableRefObject<boolean>
  run: AgentRunRecord
}) {
  const context = useAgentCanvas()
  const { locale, t } = useLocale()
  const isLive = isActiveAgentRun(run.status)
  const gate = isGateAgentRun(run.status)
  const tasks = (run.plan?.tasks ?? []).filter(
    (task) => task.toolKind !== 'synthesis',
  )
  const done = tasks.filter(
    (task) => effectiveAgentTaskStatus(task, run.taskStates[task.taskId]) === 'completed',
  ).length
  const transportDegraded = context.pollingRunIds.includes(run.runId)
  const groups = agentTaskGroups(tasks, run.taskStates)
  // Idle prefetch: settled task results of the OPEN run view warm before
  // the click (hover covers pointer users; this covers touch/keyboard
  // and the first click). The effect keys on the CANDIDATE SET, not on
  // the per-event taskStates identity — a live run streams events every
  // second and must not re-arm the idle callback for an unchanged set.
  const prefetchIdsKey = prefetchableTaskResultIds(
    run.plan?.tasks ?? [],
    run.taskStates,
    6,
  ).join(',')
  const contextRef = useRef(context)
  contextRef.current = context
  useEffect(() => {
    if (!prefetchIdsKey) return undefined
    return scheduleIdle(() => {
      for (const taskId of prefetchIdsKey.split(',')) {
        contextRef.current.prefetchTaskResult(run.runId, taskId)
      }
    }, { timeout: 800 })
  }, [prefetchIdsKey, run.runId])
  return (
    <ScrollArea className="min-h-0 flex-1">
      <div className={cn(canvasSurfaceClass, 'space-y-4')}>
        <p className="break-words t-body text-foreground/90">{run.question}</p>
        <div className="flex items-center gap-3">
          <AgentPulseTrack className="min-w-0 flex-1" run={run} withLabels />
          <StatusBadge
            className="self-start"
            density="table"
            label={t.status[run.status]}
            tone={runStatusTone(run.status)}
          />
        </div>
        {isLive && (
          <AgentActivityLine gate={gate} text={activityText(run, t, locale)} />
        )}
        {isLive && transportDegraded && (
          <p className="flex items-center gap-1.5 t-hint text-muted-foreground" role="status">
            <AlertTriangle className="icon-xs shrink-0 text-warning" />
            {t.agent.timeline.transportDegraded}
          </p>
        )}
        {run.error && (
          <p className="break-words t-meta-sm text-destructive">{run.error}</p>
        )}
        {tasks.length > 0 && (
          <div>
            <div className="mb-1.5 flex items-center justify-between">
              <span className="t-card text-foreground">
                {t.agent.timeline.tasks}
              </span>
              <span className="t-hint tabular-nums text-muted-foreground">
                {done}/{tasks.length}
              </span>
            </div>
            <div className="space-y-3">
              {groups.map((group) => (
                <section key={group.kind}>
                  <div className="mb-1 flex items-center justify-between">
                    <p className="t-caption uppercase tracking-wide text-muted-foreground">
                      {taskGroupLabel(group.kind, t)}
                    </p>
                    <span className="t-hint tabular-nums text-muted-foreground">
                      {group.tasks.length}
                    </span>
                  </div>
                  <ul className="space-y-2">
                    {group.tasks.map((task) => {
                      const live = run.taskStates[task.taskId]
                      return (
                        <li key={task.taskId}>
                          <TaskWorkUnit
                            child={childForTask(run, task, live?.childRunId)}
                            id={taskButtonId(task.taskId)}
                            live={live}
                            onClick={() => {
                              restoredFocusRef.current = false
                              context.openCanvasView({
                                runId: run.runId,
                                taskId: task.taskId,
                                view: 'run',
                              })
                            }}
                            onPrefetch={
                              TERMINAL_AGENT_TASK_STATUSES.has(
                                effectiveAgentTaskStatus(task, live),
                              )
                                ? () => context.prefetchTaskResult(
                                  run.runId,
                                  task.taskId,
                                )
                                : undefined
                            }
                            onFocusRestored={() => {
                              restoredFocusRef.current = true
                            }}
                            restoreFocus={
                              !restoredFocusRef.current
                              && descriptor.focusTaskId === task.taskId
                            }
                            task={task}
                          />
                        </li>
                      )
                    })}
                  </ul>
                </section>
              ))}
            </div>
          </div>
        )}
      </div>
    </ScrollArea>
  )
}

export function TaskWorkUnit({
  child,
  id,
  live,
  onClick,
  onFocusRestored,
  onPrefetch,
  restoreFocus,
  task,
}: {
  child: AgentChildProgressRecord | undefined
  id: string
  live: AgentRunRecord['taskStates'][string] | undefined
  onClick: () => void
  onFocusRestored: () => void
  /** Intent signal (hover/focus): warm the task-result cache so the
   * detail view opens without its fetch delay. Terminal tasks only. */
  onPrefetch?: () => void
  restoreFocus: boolean
  task: AgentPlanTaskRecord
}) {
  const { t } = useLocale()
  const buttonRef = useRef<HTMLButtonElement>(null)
  const status = effectiveAgentTaskStatus(task, live)
  const activeOperation = status === 'running' || status === 'cancel_requested'
  const requestCount = localRequestCount(task)
  const startedAtIso = live?.startedAt === undefined
    ? undefined
    : new Date(live.startedAt * 1000).toISOString()
  const runningDuration = useRunningDuration(
    activeOperation ? 'running' : 'settled',
    startedAtIso,
  )
  const elapsedSeconds = agentTaskElapsedSeconds(live, Date.now() / 1000)
  const elapsedLabel = live?.startedAt === undefined
    ? undefined
    : activeOperation
      ? runningDuration
      : elapsedSeconds === undefined
        ? undefined
        : formatDuration(elapsedSeconds)

  useLayoutEffect(() => {
    if (!restoreFocus || !buttonRef.current) return
    buttonRef.current.focus({ preventScroll: true })
    onFocusRestored()
  }, [onFocusRestored, restoreFocus])
  const snapshot = snapshotWithResearchMetrics(
    projectedChildSnapshot(child),
    live?.metrics,
    localRequestCount(task),
  )
  const phase = researchNodePhase(child?.currentNode ?? snapshot?.current_node)
  const activity = live?.activity
  const phaseMessage = child?.message
    ?? snapshot?.last_message
    ?? (activity ? activityDisplayText(activity, t) : undefined)
  const message = !activeOperation ? undefined : phaseMessage
    ?? (
      status === 'running'
      && task.toolKind === 'web_research'
      && !phase
        ? t.agent.canvas.child.preparing
        : undefined
    )
  const liveError = live?.error ?? child?.error
  const updatedAt = child?.updatedAt ?? activity?.at
  const resultSummary = task.resultSummary || live?.resultSummary
  const resultPreview = resultSummary
    ? agentTaskResultPreview(task.title, resultSummary)
    : ''
  return (
    <button
      className="group relative w-full rounded-lg border border-border bg-card py-2.5 pl-3 pr-9 text-left shadow-[0_1px_2px_var(--shadow-hairline)] transition-colors hover:bg-accent/50 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
      id={id}
      onClick={onClick}
      onFocus={onPrefetch}
      onPointerEnter={onPrefetch}
      ref={buttonRef}
      type="button"
    >
      <div className="flex min-w-0 items-start gap-2">
        <TaskStatusGlyph status={status} />
        <div className="min-w-0 flex-1">
          <p className="break-words t-list font-semibold text-foreground">
            {task.title}
          </p>
          <p className="mt-0.5 break-words t-meta-sm text-muted-foreground">
            {agentTaskExecutionLabel(task, t)}
            {(live?.attempt ?? child?.attempt ?? 1) > 1
              && ` · ${t.agent.canvas.child.attempt.replace('{count}', String(live?.attempt ?? child?.attempt))}`}
          </p>
          {/* Only locally executed query lists have a one-request-per-query
              denominator; research task queries are guiding questions. */}
          {requestCount !== undefined && requestCount > 1 && (
            <p className="mt-0.5 t-hint tabular-nums text-muted-foreground">
              {t.agent.task.queryProgress
                .replace('{count}', String(requestCount))
                .replace(
                  '{done}',
                  String(
                    (live?.activityHistory ?? []).filter(
                      (item) =>
                        item.status === 'completed'
                        || item.status === 'failed',
                    ).length,
                  ),
                )}
            </p>
          )}
        </div>
        <span className="mr-1 shrink-0">
          <StatusBadge
            density="table"
            label={agentTaskStatusLabel(status, t)}
            tone={taskStatusTone(status)}
          />
        </span>
      </div>
      <ChevronRight
        aria-hidden="true"
        className="absolute right-3 top-1/2 icon-sm -translate-y-1/2 text-muted-foreground/55 transition-[color,transform] group-hover:translate-x-0.5 group-hover:text-foreground"
      />
      {phase && (
        <div className="mt-2.5">
          <PhaseSegments
            activePhase={phase}
            completedPhases={completedResearchPhases(
              child?.currentNode ?? snapshot?.current_node,
            )}
            phases={phaseOrder}
            thin
          />
        </div>
      )}
      {task.toolKind === 'web_instant' && activeOperation && (
        <IndeterminateTaskRail />
      )}
      <TaskMetrics
        includeZeroClaims={Boolean(child) || task.toolKind === 'web_research'}
        snapshot={snapshot}
      />
      {message && (
        <p className="mt-1.5 break-words t-meta text-muted-foreground" role="status">
          {message}
        </p>
      )}
      {resultPreview && !activeOperation && (
        <p className="mt-1.5 line-clamp-3 break-words t-meta text-foreground/85">
          {resultPreview}
        </p>
      )}
      {live?.fallback && (
        <p className="mt-1.5 flex items-start gap-1.5 break-words t-meta-sm text-warning">
          <AlertTriangle className="mt-0.5 icon-xs shrink-0" />
          {t.agent.canvas.child.fallbackUsed}
        </p>
      )}
      {live?.evidenceLimit && (
        <p className="mt-1.5 flex items-start gap-1.5 break-words t-meta-sm text-muted-foreground">
          <Info className="mt-0.5 icon-xs shrink-0" />
          {t.agent.canvas.child.evidenceLimitNote}
        </p>
      )}
      {liveError && (
        <p className="mt-1.5 break-words t-meta-sm text-destructive">
          {liveError}
        </p>
      )}
      {(updatedAt !== undefined || elapsedLabel) && (
        <div className="mt-1 flex items-center justify-between gap-3 t-hint tabular-nums text-muted-foreground/70">
          <span>
            {updatedAt === undefined
              ? ''
              : t.agent.canvas.child.lastUpdate.replace(
                '{time}',
                new Date(updatedAt * 1000).toLocaleTimeString([], {
                  hour: '2-digit',
                  minute: '2-digit',
                  second: '2-digit',
                }),
              )}
          </span>
          {elapsedLabel && (
            <span className="inline-flex shrink-0 items-center gap-1">
              <Clock3 aria-hidden="true" className="icon-xs" />
              <span className="sr-only">{t.agent.canvas.child.elapsed}</span>
              {elapsedLabel}
            </span>
          )}
        </div>
      )}
    </button>
  )
}

function TaskDetailView({
  onBack,
  run,
  taskId,
}: {
  onBack: () => void
  run: AgentRunRecord
  taskId: string
}) {
  const context = useAgentCanvas()
  const { t } = useLocale()
  const task = run.plan?.tasks.find((candidate) => candidate.taskId === taskId)
  const live = run.taskStates[taskId]
  const childRunId = live?.childRunId ?? task?.childRunId ?? undefined
  const parentChild = childRunId ? run.children[childRunId] : undefined
  const parentChildRef = useRef(parentChild)
  parentChildRef.current = parentChild
  const [snapshot, setSnapshot] = useState<ResearchRunSnapshot | undefined>(
    projectedChildSnapshot(parentChild),
  )
  const [history, setHistory] = useState<ChildProgressHistory>({ messages: [], dropped: 0 })
  const messages = history.messages
  const [transport, setTransport] = useState<
    'connecting' | 'error' | 'idle' | 'polling' | 'settled' | 'sse'
  >(childRunId ? 'connecting' : 'idle')
  const [transportError, setTransportError] = useState<string | null>(null)
  const [taskResult, setTaskResult] = useState<AgentTaskResultWire | null>(null)
  const [resultLoading, setResultLoading] = useState(false)
  const [resultError, setResultError] = useState<string | null>(null)
  const [cancelling, setCancelling] = useState(false)
  const [cancelError, setCancelError] = useState<string | null>(null)
  // The push marks the covered list `inert`, which silently evicts
  // keyboard focus to <body> — land it on the Back button instead
  // (back-nav focus restore is handled by the run overview).
  const backButtonRef = useRef<HTMLButtonElement | null>(null)
  useEffect(() => {
    backButtonRef.current?.focus({ preventScroll: true })
  }, [taskId])
  const status = task
    ? effectiveAgentTaskStatus(task, live)
    : 'pending'

  useEffect(() => {
    setSnapshot((current) => mergeResearchSnapshot(
      current,
      projectedChildSnapshot(parentChild),
    ))
  }, [parentChild])

  useEffect(() => {
    setSnapshot(projectedChildSnapshot(parentChildRef.current))
    setHistory({ messages: [], dropped: 0 })
    setTransportError(null)
    if (!childRunId) {
      setTransport('idle')
      return undefined
    }
    if (!context.clientOptions) {
      setTransport('error')
      setTransportError(t.agent.canvas.child.connectionUnavailable)
      return undefined
    }
    setTransport('connecting')
    const controller = new AbortController()
    // The channel RESOLVES after onUnavailable; without this flag the
    // .then below would overwrite the error state with 'settled' one
    // microtask later.
    let unavailable = false
    void subscribeRunEvents({
      eventsUrl: `/v1/runs/${childRunId}/events`,
      options: context.clientOptions,
      signal: controller.signal,
      onTransportChange: setTransport,
      onEvent: (event: ResearchRunEvent) => {
        setSnapshot((current) => mergeResearchSnapshot(
          current,
          childEventSnapshot(event),
        ))
        const message = childProgressMessage(event, t.agent.canvas.child)
        if (message) {
          setHistory((current) => appendChildProgressMessage(current, message))
        }
      },
      onUnavailable: () => {
        // Softening the channel's rejection must not soften THIS surface:
        // a revoked/deleted child is an error here, never a calm
        // "settled" ordered end.
        if (controller.signal.aborted) return
        unavailable = true
        setTransport('error')
        setTransportError(t.agent.canvas.child.runUnavailable)
      },
    })
      .then(() => {
        if (!controller.signal.aborted && !unavailable) {
          setTransport('settled')
        }
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return
        setTransport('error')
        setTransportError(error instanceof Error ? error.message : String(error))
      })
    return () => controller.abort()
  }, [
    childRunId,
    context.clientOptions,
    t.agent.canvas.child.connectionUnavailable,
    t.agent.canvas.child.runUnavailable,
    t.agent.canvas.child,
  ])

  useEffect(() => {
    setTaskResult(null)
    setResultError(null)
    if (
      !task
      || !context.clientOptions
      || status === 'pending'
      || status === 'running'
      || status === 'cancel_requested'
    ) {
      setResultLoading(false)
      return undefined
    }
    let active = true
    setResultLoading(true)
    void context.loadTaskResult(run.runId, taskId)
      .then((result) => {
        if (active) setTaskResult(result)
      })
      .catch((loadError: unknown) => {
        if (active) {
          setResultError(
            loadError instanceof Error ? loadError.message : String(loadError),
          )
        }
      })
      .finally(() => {
        if (active) setResultLoading(false)
      })
    return () => {
      active = false
    }
  }, [context.clientOptions, context.loadTaskResult, run.runId, status, task, taskId])

  if (!task) return <CanvasMissing label={t.agent.canvas.views.run} />
  const child = childRunId ? run.children[childRunId] : undefined
  const phase = researchNodePhase(child?.currentNode ?? snapshot?.current_node)
  const liveError = live?.error ?? child?.error
  const currentMessage = child?.message ?? snapshot?.last_message
  const localActivity = live?.activity
  const localActivityHistory = live?.activityHistory ?? []
  const liveMetricsSnapshot = snapshotWithResearchMetrics(
    snapshot,
    live?.metrics,
    localRequestCount(task),
  )
  const resultSummary = task.resultSummary || live?.resultSummary
  const resultContent = agentTaskResultContent(
    taskResult,
    resultSummary ?? '',
    resultError,
  )
  const resultMarkdown = resultContent.markdown
  const resultIsPreviewOnly = resultContent.previewOnly
  const metricsSnapshot = taskResult
    ? snapshotWithResearchMetrics(
      liveMetricsSnapshot,
      taskResult.metrics,
      localRequestCount(task),
    )
    : liveMetricsSnapshot
  const displayedError = taskResult?.error?.message ?? liveError
  const duplicatedTerminalErrorIndex = terminalActivityErrorIndex(
    localActivityHistory,
    displayedError,
  )
  const canCancel = canEditAgentRun(run) && Boolean(context.clientOptions) && (
    status === 'pending' || status === 'running'
  )
  const cancellationPending = cancelling || status === 'cancel_requested'

  const requestTaskCancel = async () => {
    setCancelling(true)
    setCancelError(null)
    try {
      await context.cancelTask(run.runId, task.taskId)
    } catch (requestError) {
      setCancelError(
        requestError instanceof Error ? requestError.message : String(requestError),
      )
    } finally {
      setCancelling(false)
    }
  }

  return (
    <ScrollArea className="min-h-0 flex-1">
      <div className={cn(canvasSurfaceClass, 'space-y-4')}>
        <Button
          className="gap-1.5 px-2 text-muted-foreground hover:text-foreground"
          onClick={onBack}
          ref={backButtonRef}
          size="sm"
          type="button"
          variant="ghost"
        >
          <ChevronLeft className="icon-sm" />
          {t.agent.canvas.child.back}
        </Button>
        <div className="flex min-w-0 items-start gap-2">
          <TaskStatusGlyph status={status} />
          <div className="min-w-0 flex-1">
            <p className="break-words t-card text-foreground">{task.title}</p>
            {task.objective && (
              <p className="mt-0.5 break-words t-meta text-muted-foreground">
                {task.objective}
              </p>
            )}
            <p className="mt-1 t-meta-sm text-muted-foreground">
              {agentTaskExecutionLabel(task, t)}
            </p>
          </div>
          <div className="flex shrink-0 items-center gap-1.5">
            {(canCancel || cancellationPending) && (
              <Button
                className="h-7 gap-1 px-2 text-xs text-muted-foreground hover:text-destructive"
                disabled={cancellationPending}
                onClick={() => void requestTaskCancel()}
                size="sm"
                type="button"
                variant="ghost"
              >
                <X className="icon-xs" />
                {cancellationPending
                  ? t.agent.canvas.child.cancellingTask
                  : t.agent.canvas.child.cancelTask}
              </Button>
            )}
            <StatusBadge
              density="table"
              label={agentTaskStatusLabel(status, t)}
              tone={taskStatusTone(status)}
            />
          </div>
        </div>

        {childRunId && (
          <div className="rounded-lg border border-border bg-card px-3 py-3">
            {phase ? (
              <PhaseSegments
                activePhase={phase}
                completedPhases={completedResearchPhases(
                  child?.currentNode ?? snapshot?.current_node,
                )}
                labelFor={(item) => phaseLabel(item, t)}
                phases={phaseOrder}
                withLabels
              />
            ) : (
              <p className="t-meta text-muted-foreground" role="status">
                {t.agent.canvas.child.preparing}
              </p>
            )}
            {currentMessage && (
              <p className="mt-2 break-words t-meta text-muted-foreground" role="status">
                {currentMessage}
              </p>
            )}
            <p
              className={cn(
                'mt-2 flex items-center gap-1.5 t-hint',
                transport === 'error' ? 'text-destructive' : 'text-muted-foreground',
              )}
              role="status"
            >
              {transport === 'error' && <AlertTriangle className="icon-xs shrink-0" />}
              {childTransportLabel(transport, t)}
            </p>
            {transportError && (
              <p className="mt-1 break-words t-meta-sm text-destructive">
                {transportError}
              </p>
            )}
          </div>
        )}

        {!childRunId && (localActivity || status === 'running') && (
          <div className="rounded-lg border border-border bg-card px-3 py-3">
            {localActivity ? (
              <p className="break-words t-meta text-muted-foreground" role="status">
                {activityDisplayText(localActivity, t)}
              </p>
            ) : (
              <p className="t-meta text-muted-foreground" role="status">
                {t.agent.canvas.child.preparing}
              </p>
            )}
            {task.toolKind === 'web_instant'
              && (status === 'running' || status === 'cancel_requested') && (
              <IndeterminateTaskRail />
            )}
          </div>
        )}

        <TaskMetrics
          includeZeroClaims={Boolean(childRunId) || task.toolKind === 'web_research'}
          snapshot={metricsSnapshot}
        />

        {!childRunId && localActivityHistory.length > 0 && (
          <div>
            <p className="t-caption uppercase tracking-wide text-muted-foreground">
              {t.agent.canvas.child.operations}
            </p>
            <ul className="mt-1.5 space-y-1.5">
              {localActivityHistory.map((activity, index) => {
                const activityError = index === duplicatedTerminalErrorIndex
                  ? undefined
                  : activity.error
                const attention = Boolean(
                  activityError
                  || activity.fallback
                  || activity.status === 'failed',
                )
                return (
                  <li
                    className={cn(
                      'flex items-start gap-1.5 break-words t-meta-sm',
                      attention ? 'text-warning' : 'text-muted-foreground',
                    )}
                    key={activity.activityId ?? `${activity.at}-${index}`}
                  >
                    {attention ? (
                      <AlertTriangle className="mt-0.5 icon-xs shrink-0" />
                    ) : activity.evidenceLimit ? (
                      <Info className="mt-0.5 icon-xs shrink-0" />
                    ) : null}
                    <span>
                      {activityDisplayText(activity, t)}
                      {activityError ? ` · ${activityError}` : ''}
                    </span>
                  </li>
                )
              })}
            </ul>
          </div>
        )}

        {messages.length > 0 && (
          <div>
            <p className="t-caption uppercase tracking-wide text-muted-foreground">
              {t.agent.canvas.child.messages}
            </p>
            {history.dropped > 0 && <p className="t-hint text-muted-foreground">{countLabel(history.dropped, t.agent.canvas.child.messagesDroppedOne, t.agent.canvas.child.messagesDropped)}</p>}
            <ul className="mt-1.5 space-y-1.5">
              {messages.map((message) => (
                <li
                  className={cn(
                    'flex items-start gap-1.5 break-words t-meta-sm',
                    message.severity === 'error'
                      ? 'text-destructive'
                      : message.severity === 'warning'
                        ? 'text-warning'
                        : 'text-muted-foreground',
                  )}
                  key={message.queryId ?? message.sequence}
                >
                  {message.severity !== 'info' && (
                    <AlertTriangle className="mt-0.5 icon-xs shrink-0" />
                  )}
                  <span>{message.text}{message.query ? ` · ${message.query}` : ''}</span>
                </li>
              ))}
            </ul>
          </div>
        )}
        {resultLoading && (
          <div
            className="rounded-lg border border-border bg-surface/50 px-3 py-2.5"
            role="status"
          >
            <span className="sr-only">
              {t.agent.canvas.child.resultLoading}
            </span>
            <div aria-hidden="true" className="space-y-2">
              <Skeleton className="h-4 w-[86%]" />
              <Skeleton className="h-4 w-[72%]" />
              <Skeleton className="h-4 w-[64%]" />
            </div>
          </div>
        )}
        {resultMarkdown && (
          <div className="rounded-lg border border-border bg-surface/50 px-3 py-2.5">
            <p className="t-caption uppercase tracking-wide text-muted-foreground">
              {t.agent.canvas.child.result}
            </p>
            <MarkdownSelectionCopyMenu
              aiGenerated
              className="mt-1 min-w-0 text-sm leading-snug text-foreground/90"
              markdown={resultMarkdown}
            >
              <MarkdownRenderer
                markdown={resultMarkdown}
                variant="chat"
              />
            </MarkdownSelectionCopyMenu>
            {taskResult && taskResult.references.length > 0 && (
              <TaskResultSources references={taskResult.references} />
            )}
          </div>
        )}
        {resultIsPreviewOnly && (
          <p className="flex items-start gap-1.5 break-words t-meta text-warning">
            <AlertTriangle className="mt-0.5 icon-xs shrink-0" />
            {t.agent.canvas.child.resultPreviewOnly}
          </p>
        )}
        {resultError && (
          <p className="break-words t-meta text-warning">
            {t.agent.canvas.child.resultUnavailable} · {resultError}
          </p>
        )}
        {cancelError && (
          <p className="break-words t-meta text-destructive">{cancelError}</p>
        )}
        {live?.fallback && (
          <p className="flex items-start gap-1.5 break-words t-meta text-warning">
            <AlertTriangle className="mt-0.5 icon-xs shrink-0" />
            {t.agent.canvas.child.fallbackUsed}
          </p>
        )}
        {live?.evidenceLimit && (
          <p className="flex items-start gap-1.5 break-words t-meta text-muted-foreground">
            <Info className="mt-0.5 icon-xs shrink-0" />
            {t.agent.canvas.child.evidenceLimitNote}
          </p>
        )}
        {displayedError && (
          <p className="flex items-start gap-1.5 break-words t-meta text-destructive">
            <AlertTriangle className="mt-0.5 icon-xs shrink-0" />
            {displayedError}
          </p>
        )}
        <details className="rounded-md border border-border bg-surface/40 px-3 py-2">
          <summary className="cursor-pointer t-meta font-medium text-muted-foreground">
            {t.agent.canvas.child.technicalDetails}
          </summary>
          <dl className="mt-2 grid grid-cols-[max-content_minmax(0,1fr)] gap-x-3 gap-y-1 t-hint text-muted-foreground">
            <dt>{t.agent.canvas.child.detailChildRun}</dt>
            <dd className="min-w-0 break-all t-mono text-foreground/80">
              {childRunId ?? '—'}
            </dd>
            <dt>{t.agent.canvas.child.detailOperation}</dt>
            <dd className="min-w-0 break-all t-mono text-foreground/80">
              {localActivity?.operationCode
                ?? localActivity?.operation
                ?? task.toolKind}
            </dd>
            <dt>{t.agent.canvas.child.detailAttempt}</dt>
            <dd className="tabular-nums text-foreground/80">
              {live?.attempt ?? child?.attempt ?? 1}
            </dd>
            <dt>{t.agent.canvas.child.detailTransport}</dt>
            <dd className="text-foreground/80">{childTransportLabel(transport, t)}</dd>
            <dt>{t.agent.canvas.child.detailFallback}</dt>
            <dd className="text-foreground/80">
              {live?.fallback
                ? t.agent.canvas.child.detailYes
                : t.agent.canvas.child.detailNo}
            </dd>
            {(taskResult?.error?.code ?? live?.errorCode ?? child?.errorCode) && (
              <>
                <dt>{t.agent.canvas.child.detailErrorCode}</dt>
                <dd className="min-w-0 break-all t-mono text-foreground/80">
                  {taskResult?.error?.code ?? live?.errorCode ?? child?.errorCode}
                </dd>
              </>
            )}
            {taskResult && (
              <>
                <dt>{t.agent.canvas.child.detailPromptTokens}</dt>
                <dd className="tabular-nums text-foreground/80">
                  {taskResult.metrics.prompt_tokens}
                </dd>
                <dt>{t.agent.canvas.child.detailCompletionTokens}</dt>
                <dd className="tabular-nums text-foreground/80">
                  {taskResult.metrics.completion_tokens}
                </dd>
              </>
            )}
          </dl>
        </details>
      </div>
    </ScrollArea>
  )
}

function TaskResultSources({
  references,
}: {
  references: readonly Record<string, unknown>[]
}) {
  const { t } = useLocale()
  const groups = taskResultReferenceGroups(references)
  if (groups.length === 0) return null
  return (
    <section className="mt-3 border-t border-border/70 pt-2.5">
      <p className="t-caption uppercase tracking-wide text-muted-foreground">
        {t.knowledge.sources}
      </p>
      <ul className="mt-1.5 space-y-2">
        {groups.map((group) => {
          if (group.kind === 'web') {
            return (
              <li key={group.reference.key}>
                <WebEvidenceSourceRow reference={group.reference} />
              </li>
            )
          }
          const several = group.references.length > 1
          return (
            <li key={`document:${group.references[0]?.documentId ?? group.title}`}>
              {several && (
                <div className="flex min-w-0 items-center gap-1.5">
                  <FileText className="icon-sm shrink-0 text-muted-foreground/70" />
                  <span className="min-w-0 flex-1 truncate t-list text-foreground">
                    {group.title}
                  </span>
                  <span className="t-hint tabular-nums text-muted-foreground">
                    {group.references.length}
                  </span>
                </div>
              )}
              <ul className={cn('space-y-1', several && 'mt-1 border-l border-border/60 pl-2')}>
                {group.references.map((reference) => (
                  <li className="min-w-0" key={reference.key}>
                    {!several && (
                      <p className="t-list text-foreground">{reference.title}</p>
                    )}
                    {reference.excerpt && (
                      <p className="line-clamp-3 break-words t-meta text-foreground/85">
                        {reference.excerpt}
                      </p>
                    )}
                    {(reference.pageNumber !== null || reference.chunkIndex !== null) && (
                      <p className="mt-0.5 t-meta-sm text-muted-foreground">
                        {[
                          reference.pageNumber === null
                            ? null
                            : t.knowledge.citationPage.replace(
                              '{n}',
                              String(reference.pageNumber),
                            ),
                          reference.chunkIndex === null
                            ? null
                            : t.knowledge.viewerSection.replace(
                              '{n}',
                              String(reference.chunkIndex + 1),
                            ),
                        ].filter(Boolean).join(' · ')}
                      </p>
                    )}
                  </li>
                ))}
              </ul>
            </li>
          )
        })}
      </ul>
    </section>
  )
}

function TaskMetrics({
  includeZeroClaims,
  snapshot,
}: {
  includeZeroClaims: boolean
  snapshot: ResearchRunSnapshot | undefined
}) {
  const { t } = useLocale()
  const metrics = agentTaskMetrics(snapshot, includeZeroClaims)
  if (metrics.length === 0) return null
  return (
    <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 t-meta-sm tabular-nums text-muted-foreground">
      {metrics.map((metric) => (
        <span key={metric.kind}>
          <strong className="font-semibold text-foreground">{metric.value}</strong>{' '}
          {metric.kind === 'sources'
            ? t.runCard.sources
            : metric.kind === 'queries'
              ? t.runCard.queries
              : t.runCard.claims}
        </span>
      ))}
    </div>
  )
}

function TaskStatusGlyph({ status }: { status: AgentTaskEffectiveStatus }) {
  if (status === 'completed') {
    return <Check className="mt-0.5 icon-sm shrink-0 text-success" />
  }
  if (status === 'failed') {
    return <X className="mt-0.5 icon-sm shrink-0 text-destructive" />
  }
  if (status === 'insufficient_evidence') {
    return <AlertTriangle className="mt-0.5 icon-sm shrink-0 text-warning" />
  }
  if (status === 'cancelled') {
    return <X className="mt-0.5 icon-sm shrink-0 text-muted-foreground" />
  }
  return (
    <span
      aria-hidden="true"
      className={cn(
        'mt-1.5 size-1.5 shrink-0 rounded-full',
        status === 'running' || status === 'cancel_requested'
          ? 'bg-brand inqtrix-running-dot'
          : 'bg-muted-foreground/30',
      )}
    />
  )
}

function childForTask(
  run: AgentRunRecord,
  task: AgentPlanTaskRecord,
  liveChildRunId: string | undefined,
): AgentChildProgressRecord | undefined {
  const childRunId = liveChildRunId ?? task.childRunId ?? undefined
  if (childRunId) return run.children[childRunId]
  return Object.values(run.children).find((child) => child.taskId === task.taskId)
}

function projectedChildSnapshot(
  child: AgentChildProgressRecord | undefined,
): ResearchRunSnapshot | undefined {
  if (!child) return undefined
  const projected: ResearchRunSnapshot = {
    ...(child.snapshot ?? {}),
    ...(child.currentNode ? { current_node: child.currentNode } : {}),
    ...(child.message ? { last_message: child.message } : {}),
  }
  return snapshotWithResearchMetrics(
    Object.keys(projected).length > 0 ? projected : undefined,
    child.metrics,
  )
}

function localRequestCount(task: AgentPlanTaskRecord): number | undefined {
  if (task.toolKind !== 'web_instant' && task.toolKind !== 'rag_query') {
    return undefined
  }
  return agentTaskExecutionSemantics(task).requestCount
}

function childEventSnapshot(event: ResearchRunEvent): ResearchRunSnapshot | undefined {
  const snapshot = mergeResearchSnapshot(undefined, event.data.snapshot)
  const currentNode = typeof event.data.current_node === 'string'
    ? event.data.current_node
    : undefined
  const message = typeof event.data.message === 'string'
    ? event.data.message
    : undefined
  if (!snapshot && !currentNode && !message) return undefined
  return {
    ...(snapshot ?? {}),
    ...(currentNode ? { current_node: currentNode } : {}),
    ...(message ? { last_message: message } : {}),
  }
}

function childTransportLabel(
  transport: 'connecting' | 'error' | 'idle' | 'polling' | 'settled' | 'sse',
  t: ReturnType<typeof useLocale>['t'],
): string {
  if (transport === 'sse') return t.agent.canvas.child.connectionLive
  if (transport === 'polling') return t.agent.canvas.child.connectionPolling
  if (transport === 'error') return t.agent.canvas.child.connectionError
  if (transport === 'settled') return t.agent.canvas.child.connectionSettled
  if (transport === 'idle') return t.agent.canvas.child.connectionIdle
  return t.agent.canvas.child.connectionConnecting
}

function taskGroupLabel(
  kind: AgentTaskGroup,
  t: ReturnType<typeof useLocale>['t'],
): string {
  if (kind === 'attention') return t.agent.canvas.taskGroups.attention
  if (kind === 'completed') return t.agent.canvas.taskGroups.completed
  return t.agent.canvas.taskGroups.active
}

function taskStatusTone(status: AgentTaskEffectiveStatus): StatusTone {
  if (status === 'completed') return 'success'
  if (status === 'failed') return 'destructive'
  if (status === 'insufficient_evidence') return 'warning'
  if (status === 'cancel_requested') return 'warning'
  if (status === 'running') return 'brand'
  return 'neutral'
}

function runStatusTone(status: AgentRunRecord['status']): StatusTone {
  if (status === 'completed') return 'success'
  if (status === 'failed' || status === 'cancelled') return 'destructive'
  if (status === 'waiting_for_approval' || status === 'waiting_for_input') {
    return 'warning'
  }
  if (isActiveAgentRun(status)) return 'brand'
  return 'neutral'
}

function taskButtonId(taskId: string): string {
  return `agent-task-${encodeURIComponent(taskId)}`
}

function IndeterminateTaskRail() {
  return (
    <div
      aria-hidden="true"
      className="mt-2 h-1 overflow-hidden rounded-full bg-brand/15"
    >
      <span className="inqtrix-segment-breathe block h-full rounded-full bg-brand" />
    </div>
  )
}

function CanvasMissing({ label }: { label: string }) {
  return (
    <div className="flex min-h-0 flex-1 items-center justify-center px-6">
      <p className="t-meta text-muted-foreground">{label}</p>
    </div>
  )
}

function noop() {}
async function asyncNoop() {}

const emptyRunPlaceholder = {
  runId: '',
  kind: 'agent',
  question: '',
  status: 'queued',
  phase: 'intake',
  station: 'intake',
  createdAt: '',
  lastSequence: 0,
  planStale: false,
  approvals: [],
  approvalsStale: false,
  clarifications: [],
  clarificationsStale: false,
  artifactOrder: [],
  artifacts: {},
  artifactsStale: false,
  taskStates: {},
  children: {},
  patchStale: false,
} as unknown as AgentRunRecord


/**
 * The Agent Desk view registry — a MODULE CONSTANT: renderer identities
 * never change across workspace renders, so the active view (editor
 * cursor, task SSE subscription) survives state updates. Data flows via
 * `AgentCanvasReactContext`.
 */
export const AGENT_CANVAS_REGISTRY = {
  diff: DiffCanvasView,
  document: DocumentCanvasView,
  evidence: EvidenceCanvasView,
  file: FileCanvasView,
  patch: PatchCanvasView,
  plan: PlanCanvasView,
  run: RunCanvasView,
} as const
