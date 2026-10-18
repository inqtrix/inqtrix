import { AlertTriangle, Check, Sparkles, X } from '@/components/icons'
import { Button } from '@/components/ui/button'
import type { EditorSuggestionRecord } from '@/features/project/types'
import { markdownToPlainTextForEditor } from './anchoring'
import { SuggestionErrorLine } from './SuggestionErrorLine'

type EditorDocumentChangesLabels = {
  accept: string
  acceptAll: string
  documentChanges: string
  proposedChange: string
  reject: string
  rejectAll: string
  /** Shown on a card whose passage changed since the suggestion was made. */
  stale: string
}

export function EditorDocumentChangesSection({
  labels,
  onAcceptGroup,
  onAcceptSuggestion,
  onRejectGroup,
  onRejectSuggestion,
  onSelectSuggestion,
  publishDisabledReason = null,
  suggestionErrors,
  suggestions,
}: {
  labels: EditorDocumentChangesLabels
  onAcceptGroup: (groupId: string) => void
  onAcceptSuggestion: (suggestion: EditorSuggestionRecord) => void
  onRejectGroup: (groupId: string) => void
  onRejectSuggestion: (suggestionId: string) => void
  onSelectSuggestion: (suggestionId: string) => void
  publishDisabledReason?: string | null
  suggestionErrors: Record<string, string>
  suggestions: EditorSuggestionRecord[]
}) {
  if (suggestions.length === 0) return null
  const groups = groupSuggestionsByGroupId(suggestions)
  return (
    <div className="space-y-2 rounded-md border border-brand/20 bg-brand-subtle/20 p-2.5">
      <div className="flex items-center gap-2">
        <Sparkles className="size-3.5 text-brand" />
        <h3 className="t-label min-w-0 flex-1 text-foreground">{labels.documentChanges}</h3>
        <span className="t-meta-sm tabular-nums text-muted-foreground">{suggestions.length}</span>
      </div>
      {groups.map((group) => {
        const pendingCount = group.suggestions.filter((suggestion) => suggestion.status === 'pending').length
        // A changed passage can no longer be accepted, but it can be
        // rejected, as on its own card.
        const rejectableCount = group.suggestions.filter((suggestion) => (
          suggestion.status === 'pending' || suggestion.status === 'stale'
        )).length
        return (
          <div className="rounded-md border border-border bg-background p-2" key={group.groupId}>
            <div className="mb-2 flex items-center justify-end gap-1.5">
              <Button
                className="h-7"
                disabled={rejectableCount === 0 || Boolean(publishDisabledReason)}
                onClick={() => onRejectGroup(group.groupId)}
                size="sm"
                title={publishDisabledReason ?? undefined}
                type="button"
                variant="ghost"
              >
                <X className="size-3.5" />
                {labels.rejectAll}
              </Button>
              <Button
                className="h-7 bg-brand text-brand-foreground hover:bg-brand/90 hover:text-brand-foreground"
                disabled={pendingCount === 0 || Boolean(publishDisabledReason)}
                onClick={() => onAcceptGroup(group.groupId)}
                size="sm"
                title={publishDisabledReason ?? undefined}
                type="button"
              >
                <Check className="size-3.5" />
                {labels.acceptAll}
              </Button>
            </div>
            <div className="space-y-1.5">
              {group.suggestions.map((suggestion, index) => (
                <DocumentChangeCard
                  error={suggestionErrors[suggestion.id]}
                  index={index + 1}
                  key={suggestion.id}
                  labels={labels}
                  onAccept={onAcceptSuggestion}
                  onReject={onRejectSuggestion}
                  onSelect={onSelectSuggestion}
                  publishDisabledReason={publishDisabledReason}
                  suggestion={suggestion}
                />
              ))}
            </div>
          </div>
        )
      })}
    </div>
  )
}

function DocumentChangeCard({
  error,
  index,
  labels,
  onAccept,
  onReject,
  onSelect,
  publishDisabledReason,
  suggestion,
}: {
  error: string | undefined
  index: number
  labels: EditorDocumentChangesLabels
  onAccept: (suggestion: EditorSuggestionRecord) => void
  onReject: (suggestionId: string) => void
  onSelect: (suggestionId: string) => void
  publishDisabledReason: string | null
  suggestion: EditorSuggestionRecord
}) {
  const anchorText = markdownToPlainTextForEditor(suggestion.anchorText || suggestion.originalText)
  const proposedPreview = markdownToPlainTextForEditor(suggestion.proposedText)
  const isStale = suggestion.status === 'stale'
  return (
    <div
      className="cursor-pointer rounded-md border border-border bg-surface/40 p-2 transition-colors hover:bg-surface/70 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      onClick={(event) => {
        const target = event.target instanceof HTMLElement ? event.target : null
        if (target?.closest('button')) return
        onSelect(suggestion.id)
      }}
      onKeyDown={(event) => {
        if (event.key !== 'Enter' && event.key !== ' ') return
        event.preventDefault()
        onSelect(suggestion.id)
      }}
      role="button"
      tabIndex={0}
    >
      <div className="flex items-center gap-1.5">
        <span className="grid size-5 shrink-0 place-items-center rounded-[5px] bg-brand-subtle t-hint font-semibold tabular-nums text-brand">
          {index}
        </span>
        <span className="t-list min-w-0 flex-1 truncate text-foreground">
          {suggestion.changeSummary?.[0] ?? labels.proposedChange}
        </span>
        {isStale ? <AlertTriangle className="size-3.5 text-warning" /> : null}
      </div>
      {anchorText ? (
        <p className="t-meta mt-1 line-clamp-2 text-muted-foreground">“{compactQuote(anchorText, 140)}”</p>
      ) : null}
      <p className="t-meta mt-1 line-clamp-3 text-foreground">
        {compactQuote(proposedPreview, 220)}
      </p>
      {suggestion.warnings?.length ? (
        <p className="t-meta-sm mt-1 text-warning">{suggestion.warnings[0]}</p>
      ) : null}
      {error ? (
        <SuggestionErrorLine message={error} />
      ) : isStale ? (
        // A changed passage is a state of the suggestion, not a failure: it
        // reads in the calm warning tone of the card, without an alert.
        <p className="t-meta-sm mt-1 text-warning" data-suggestion-stale="true" role="status">
          {labels.stale}
        </p>
      ) : null}
      <div className="mt-2 flex items-center justify-end gap-1.5">
        <Button
          className="h-7"
          disabled={Boolean(publishDisabledReason)}
          onClick={() => onReject(suggestion.id)}
          size="sm"
          title={publishDisabledReason ?? undefined}
          type="button"
          variant="ghost"
        >
          <X className="size-3.5" />
          {labels.reject}
        </Button>
        <Button
          className="h-7 bg-brand text-brand-foreground hover:bg-brand/90 hover:text-brand-foreground"
          disabled={isStale || Boolean(publishDisabledReason)}
          onClick={() => onAccept(suggestion)}
          size="sm"
          title={publishDisabledReason ?? undefined}
          type="button"
        >
          <Check className="size-3.5" />
          {labels.accept}
        </Button>
      </div>
    </div>
  )
}

function groupSuggestionsByGroupId(suggestions: EditorSuggestionRecord[]) {
  const groups = new Map<string, EditorSuggestionRecord[]>()
  for (const suggestion of suggestions) {
    groups.set(suggestion.groupId, [...(groups.get(suggestion.groupId) ?? []), suggestion])
  }
  return [...groups.entries()].map(([groupId, items]) => ({
    groupId,
    suggestions: items.sort((a, b) => a.anchor.from - b.anchor.from || a.createdAt.localeCompare(b.createdAt)),
  }))
}

function compactQuote(value: string, maxLength: number): string {
  const text = value.replace(/\s+/g, ' ').trim()
  if (text.length <= maxLength) return text
  return `${text.slice(0, Math.max(0, maxLength - 1)).trimEnd()}…`
}
