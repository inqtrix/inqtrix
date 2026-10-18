import type { ResearchRunEvent, ResearchRunStatus } from './types'

const TERMINAL_STATUSES = new Set<ResearchRunStatus>([
  'completed',
  'failed',
  'cancelled',
  'expired',
])

export function isTerminalRunStatus(status: ResearchRunStatus) {
  return TERMINAL_STATUSES.has(status)
}

export function isTerminalRunEvent(event: ResearchRunEvent) {
  return (
    event.type === 'inqtrix.run.completed' ||
    event.type === 'inqtrix.run.failed' ||
    event.type === 'inqtrix.run.cancelled'
  )
}
