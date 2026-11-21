import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
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
import {
  AlertTriangle,
  BookOpen,
  Check,
  ChevronLeft,
  Copy,
  Download,
  ExternalLink,
  FileText,
  Globe2,
  LoaderCircle,
  PenLine,
} from '@/components/icons'
import { MarkdownRenderer } from '@/components/markdown/MarkdownRenderer'
import { MarkdownSelectionCopyMenu } from '@/components/markdown/MarkdownSelectionCopyMenu'
import { Button } from '@/components/ui/button'
import { ScrollArea } from '@/components/ui/scroll-area'
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
import type { FileAssetRecord } from '@/features/project/types'
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
import { withAiDisclosure } from '@/lib/aiDisclosure'
import { cn } from '@/lib/utils'
import { AgentActivityLine, AgentPulseTrack } from '../AgentPulseTrack'
import {
  canEditAgentRun,
  isActiveAgentRun,
  isGateAgentRun,
  type AgentRunRecord,
  resolveAgentArtifact,
} from '../model'
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
import { runFailureText } from '@/lib/runFailure'
import { artifactClaimMarks, documentOutcome } from '../runPresentation'
import { ClaimNote, useClaimNote } from '../claimNote'
import { useAgentCanvas } from './context'
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
            aria-label={t.agent.canvas.versionHistory}
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

  const collectionLabel = context.collections.find(
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

// --- run (Verlauf) -------------------------------------------------------------

/** The live run view is the Agent Desk control room: the question, the
 * pulse track, what the run does right now, and how it ended. */
export function RunCanvasView({
  descriptor,
}: {
  descriptor: Extract<CanvasViewDescriptor, { view: 'run' }>
}) {
  const context = useAgentCanvas()
  const { t } = useLocale()
  const run = context.runs[descriptor.runId]
  if (!run) return <CanvasMissing label={t.agent.canvas.runUnavailable} />
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <RunOverview run={run} />
    </div>
  )
}

function RunOverview({ run }: { run: AgentRunRecord }) {
  const context = useAgentCanvas()
  const { locale, t } = useLocale()
  const isLive = isActiveAgentRun(run.status)
  const gate = isGateAgentRun(run.status)
  const transportDegraded = context.pollingRunIds.includes(run.runId)
  const failureText = runFailureText(run.error ?? '', t, run.errorCode)
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
        {failureText && (
          <p className="break-words t-meta-sm text-destructive">{failureText}</p>
        )}
      </div>
    </ScrollArea>
  )
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

function CanvasMissing({ label }: { label: string }) {
  return (
    <div className="flex min-h-0 flex-1 items-center justify-center px-6">
      <p className="t-meta text-muted-foreground">{label}</p>
    </div>
  )
}

function noop() {}
async function asyncNoop() {}

/**
 * The Agent Desk view registry — a MODULE CONSTANT: renderer identities
 * never change across workspace renders, so the active view (editor
 * cursor) survives state updates. Data flows via
 * `AgentCanvasReactContext`.
 */
export const AGENT_CANVAS_REGISTRY = {
  diff: DiffCanvasView,
  document: DocumentCanvasView,
  evidence: EvidenceCanvasView,
  file: FileCanvasView,
  run: RunCanvasView,
} as const
