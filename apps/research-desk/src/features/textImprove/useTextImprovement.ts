import { useEffect, useRef, useState } from 'react'
import { improveText } from '@/api/inqtrixClient'
import type {
  TextImprovementApiOptions,
  TextImprovementContext,
  TextImprovementMessages,
  TextImprovementProposal,
} from './types'
import { messageFromError } from '@/api/requestErrorText'

const SENSITIVE_TEXT_PATTERNS = [
  /-----BEGIN [A-Z ]*PRIVATE KEY-----/,
  /\bAKIA[0-9A-Z]{16}\b/,
  /\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}\b/,
  /\b(api[_-]?key|secret|token|password)\b\s*[:=]\s*['"]?[A-Za-z0-9_./+=-]{20,}/i,
]

export function useTextImprovement({
  apiKey,
  enabled,
  locale,
  messages,
  selectedStack,
  workspaceId,
}: TextImprovementApiOptions & {
  messages: TextImprovementMessages
}) {
  const [isImproving, setIsImproving] = useState(false)
  const [proposal, setProposal] = useState<TextImprovementProposal | null>(null)
  const abortControllerRef = useRef<AbortController | null>(null)
  const requestIdRef = useRef(0)

  useEffect(() => () => {
    abortControllerRef.current?.abort()
  }, [])

  function clearProposal() {
    setProposal(null)
  }

  async function improve(context: TextImprovementContext, text: string, guidance?: string) {
    const sourceText = text.trim()
    if (!sourceText || isImproving) return
    if (!enabled) {
      throw new Error(messages.unavailable)
    }
    if (looksLikeSensitiveText(sourceText)) {
      throw new Error(messages.sensitiveText)
    }

    const requestId = requestIdRef.current + 1
    requestIdRef.current = requestId
    abortControllerRef.current?.abort()
    const controller = new AbortController()
    abortControllerRef.current = controller
    setIsImproving(true)

    try {
      const response = await improveText(
        {
          context,
          guidance,
          locale,
          stack: selectedStack,
          text: sourceText,
        },
        {
          apiKey,
          signal: controller.signal,
          workspaceId,
        },
      )
      if (requestIdRef.current !== requestId) return
      setProposal({
        changeSummary: response.change_summary,
        clarificationQuestions: response.clarification_questions,
        improvedText: response.improved_text,
        needsClarification: response.needs_clarification,
        originalText: text,
        warnings: response.warnings,
      })
    } catch (error) {
      if (controller.signal.aborted) return
      throw new Error(messages.requestFailed(messageFromError(error)), { cause: error })
    } finally {
      if (requestIdRef.current === requestId) {
        setIsImproving(false)
        abortControllerRef.current = null
      }
    }
  }

  return {
    clearProposal,
    improve,
    isImproving,
    proposal,
  }
}

function looksLikeSensitiveText(text: string) {
  return SENSITIVE_TEXT_PATTERNS.some((pattern) => pattern.test(text))
}
