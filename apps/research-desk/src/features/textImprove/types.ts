import type { TextImprovementContext } from '@/api/inqtrixClient'
import type { Locale } from '@/i18n/translations'

export type { TextImprovementContext }

export type TextImprovementApiOptions = {
  apiKey?: string
  enabled: boolean
  locale: Locale
  selectedStack?: string
  workspaceId: string
}

export type TextImprovementMessages = {
  requestFailed: (message: string) => string
  sensitiveText: string
  unavailable: string
}

export type TextImprovementProposal = {
  changeSummary: string[]
  clarificationQuestions: string[]
  improvedText: string
  needsClarification: boolean
  originalText: string
  warnings: string[]
}

export type TextImproveReviewLabels = {
  accept: string
  changes: string
  noChanges: string
  reject: string
  title: string
  warnings: string
}
