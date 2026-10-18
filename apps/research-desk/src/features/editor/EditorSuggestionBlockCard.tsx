import { useEffect, useState, type MouseEvent, type ReactNode } from 'react'
import { Check, LoaderCircle, MessageSquareText, PencilLine, SendHorizontal, X } from '@/components/icons'
import Markdown, { type Components } from 'react-markdown'
import rehypeKatex from 'rehype-katex'
import remarkGfm from 'remark-gfm'
import remarkMath from 'remark-math'

type SuggestionBlockLabels = {
  accept: string
  cancel: string
  edit: string
  proposed: string
  refine: string
  refinementPlaceholder: string
  reject: string
  revision: string
  running: string
  save: string
  send: string
  stop: string
}

export type EditorSuggestionBlockCardProps = {
  active: boolean
  error?: string
  id: string
  isRunning: boolean
  labels: SuggestionBlockLabels
  onAccept?: (suggestionId: string) => void
  onCancelRun?: (suggestionId: string) => void
  onEdit?: (suggestionId: string, proposedText: string) => void
  onRefine?: (suggestionId: string, instruction: string) => void
  onReject?: (suggestionId: string) => void
  onSelect?: (suggestionId: string) => void
  providerActionsDisabled?: boolean
  proposedText: string
  reviewSurface: 'editor' | 'panel'
  revision: number
}

const SUGGESTION_MARKDOWN_COMPONENTS: Components = {
  a: ({ className, href, node, ...props }) => {
    void node
    return (
      <a
        {...props}
        className={joinClassNames('suggestion-markdown-link', className)}
        href={href}
        rel={props.rel ?? 'noreferrer'}
        target={props.target ?? '_blank'}
      />
    )
  },
  blockquote: ({ className, node, ...props }) => {
    void node
    return <blockquote {...props} className={joinClassNames('suggestion-markdown-blockquote', className)} />
  },
  code: ({ className, node, ...props }) => {
    void node
    return <code {...props} className={joinClassNames('suggestion-markdown-code', className)} />
  },
  h1: ({ className, node, ...props }) => {
    void node
    return <h1 {...props} className={joinClassNames('suggestion-markdown-heading suggestion-markdown-heading-1', className)} />
  },
  h2: ({ className, node, ...props }) => {
    void node
    return <h2 {...props} className={joinClassNames('suggestion-markdown-heading suggestion-markdown-heading-2', className)} />
  },
  h3: ({ className, node, ...props }) => {
    void node
    return <h3 {...props} className={joinClassNames('suggestion-markdown-heading suggestion-markdown-heading-3', className)} />
  },
  h4: ({ className, node, ...props }) => {
    void node
    return <h4 {...props} className={joinClassNames('suggestion-markdown-heading suggestion-markdown-heading-4', className)} />
  },
  li: ({ className, node, ...props }) => {
    void node
    return <li {...props} className={joinClassNames('suggestion-markdown-list-item', className)} />
  },
  ol: ({ className, node, ...props }) => {
    void node
    return <ol {...props} className={joinClassNames('suggestion-markdown-list suggestion-markdown-list-ordered', className)} />
  },
  p: ({ className, node, ...props }) => {
    void node
    return <p {...props} className={joinClassNames('suggestion-markdown-paragraph', className)} />
  },
  pre: ({ className, node, ...props }) => {
    void node
    return <pre {...props} className={joinClassNames('suggestion-markdown-pre', className)} />
  },
  strong: ({ className, node, ...props }) => {
    void node
    return <strong {...props} className={joinClassNames('suggestion-markdown-strong', className)} />
  },
  table: ({ children, className, node, ...props }) => {
    void node
    return (
      <div className="suggestion-markdown-table-wrap">
        <table {...props} className={joinClassNames('suggestion-markdown-table', className)}>{children}</table>
      </div>
    )
  },
  td: ({ className, node, ...props }) => {
    void node
    return <td {...props} className={joinClassNames('suggestion-markdown-cell', className)} />
  },
  th: ({ className, node, ...props }) => {
    void node
    return <th {...props} className={joinClassNames('suggestion-markdown-cell suggestion-markdown-header-cell', className)} />
  },
  ul: ({ className, node, ...props }) => {
    void node
    return <ul {...props} className={joinClassNames('suggestion-markdown-list suggestion-markdown-list-unordered', className)} />
  },
}

export function EditorSuggestionBlockCard({
  active,
  error,
  id,
  isRunning,
  labels,
  onAccept,
  onCancelRun,
  onEdit,
  onRefine,
  onReject,
  onSelect,
  providerActionsDisabled = false,
  proposedText,
  reviewSurface,
  revision,
}: EditorSuggestionBlockCardProps) {
  const [mode, setMode] = useState<'edit' | 'preview' | 'refine'>('preview')
  const [editDraft, setEditDraft] = useState(proposedText)
  const [refinementDraft, setRefinementDraft] = useState('')

  useEffect(() => {
    if (mode !== 'edit') setEditDraft(proposedText)
  }, [mode, proposedText])

  function handleMouseDown(event: MouseEvent<HTMLDivElement>) {
    const target = event.target instanceof HTMLElement ? event.target : null
    if (target?.closest('button, textarea, input, a')) return
    event.preventDefault()
  }

  function submitRefinement() {
    const instruction = refinementDraft.trim()
    if (!instruction || isRunning || providerActionsDisabled) return
    onRefine?.(id, instruction)
    setRefinementDraft('')
    setMode('preview')
  }

  function saveEdit() {
    if (!editDraft.trim() || isRunning) return
    onEdit?.(id, editDraft)
    setMode('preview')
  }

  return (
    <div
      className={`suggestion-block-card${active ? ' suggestion-block-card-active' : ''}`}
      onClick={(event) => {
        event.stopPropagation()
        onSelect?.(id)
      }}
      onMouseDown={handleMouseDown}
    >
      <div className="suggestion-block-header">
        <div className="suggestion-block-title">
          <span className="suggestion-block-label">{labels.proposed}</span>
          {revision > 1 ? <span className="suggestion-block-revision">{labels.revision} {revision}</span> : null}
        </div>
        <div className="suggestion-block-actions">
          <IconButton disabled={isRunning || providerActionsDisabled} label={labels.refine} onClick={() => setMode(mode === 'refine' ? 'preview' : 'refine')}>
            <MessageSquareText aria-hidden="true" className="suggestion-block-icon" />
          </IconButton>
          <IconButton disabled={isRunning} label={labels.edit} onClick={() => setMode(mode === 'edit' ? 'preview' : 'edit')}>
            <PencilLine aria-hidden="true" className="suggestion-block-icon" />
          </IconButton>
          {reviewSurface === 'editor' ? (
            <>
              <IconButton disabled={isRunning || providerActionsDisabled} label={labels.reject} onClick={() => onReject?.(id)}>
                <X aria-hidden="true" className="suggestion-block-icon" />
              </IconButton>
              <IconButton accent disabled={isRunning || providerActionsDisabled} label={labels.accept} onClick={() => onAccept?.(id)}>
                <Check aria-hidden="true" className="suggestion-block-icon" />
              </IconButton>
            </>
          ) : null}
        </div>
      </div>

      {mode === 'edit' ? (
        <div className="suggestion-block-editor">
          <textarea
            aria-label={labels.edit}
            className="suggestion-block-textarea"
            onChange={(event) => setEditDraft(event.target.value)}
            value={editDraft}
          />
          <div className="suggestion-block-inline-actions">
            <button className="suggestion-block-text-button" onClick={() => setMode('preview')} type="button">{labels.cancel}</button>
            <button className="suggestion-block-text-button suggestion-block-text-button-primary" onClick={saveEdit} type="button">{labels.save}</button>
          </div>
        </div>
      ) : (
        <SuggestionMarkdownPreview markdown={proposedText} />
      )}

      {mode === 'refine' ? (
        <form
          className="suggestion-block-refine"
          onSubmit={(event) => {
            event.preventDefault()
            submitRefinement()
          }}
        >
          <input
            aria-label={labels.refine}
            className="suggestion-block-input"
            disabled={isRunning || providerActionsDisabled}
            onChange={(event) => setRefinementDraft(event.target.value)}
            placeholder={labels.refinementPlaceholder}
            value={refinementDraft}
          />
          <IconButton accent disabled={isRunning || providerActionsDisabled || !refinementDraft.trim()} label={labels.send} type="submit">
            <SendHorizontal aria-hidden="true" className="suggestion-block-icon" />
          </IconButton>
        </form>
      ) : null}

      {isRunning ? (
        <div className="suggestion-block-status">
          <span className="suggestion-block-status-text">
            <LoaderCircle aria-hidden="true" className="suggestion-block-icon suggestion-block-spinner" />
            {labels.running}
          </span>
          <IconButton label={labels.stop} onClick={() => onCancelRun?.(id)}>
            <X aria-hidden="true" className="suggestion-block-icon" />
          </IconButton>
        </div>
      ) : null}
      {error ? <div className="suggestion-block-error">{error}</div> : null}
    </div>
  )
}

function SuggestionMarkdownPreview({ markdown }: { markdown: string }) {
  return (
    <div className="suggestion-block-proposed suggestion-markdown" data-suggestion-markdown-root="true">
      <Markdown
        components={SUGGESTION_MARKDOWN_COMPONENTS}
        rehypePlugins={[rehypeKatex]}
        remarkPlugins={[remarkGfm, remarkMath]}
        skipHtml
      >
        {normalizeSuggestionMarkdown(markdown)}
      </Markdown>
    </div>
  )
}

function IconButton({
  accent = false,
  children,
  disabled = false,
  label,
  onClick,
  type = 'button',
}: {
  accent?: boolean
  children: ReactNode
  disabled?: boolean
  label: string
  onClick?: () => void
  type?: 'button' | 'submit'
}) {
  return (
    <button
      aria-label={label}
      className={`suggestion-block-button${accent ? ' suggestion-block-button-accept' : ''}`}
      disabled={disabled}
      onClick={(event) => {
        event.stopPropagation()
        onClick?.()
      }}
      title={label}
      type={type}
    >
      {children}
    </button>
  )
}

function joinClassNames(...values: Array<string | undefined>): string {
  return values.filter(Boolean).join(' ')
}

function normalizeSuggestionMarkdown(markdown: string) {
  return markdown
    .replace(/\\\[([\s\S]*?)\\\]/g, (_match, expression: string) => (
      `\n\n$$\n${expression.trim()}\n$$\n\n`
    ))
    .replace(/\\\(([\s\S]*?)\\\)/g, (_match, expression: string) => (
      `$${expression.trim()}$`
    ))
}
