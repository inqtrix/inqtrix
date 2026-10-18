export type AgentStepScrollGeometry = {
  containerHeight: number
  stepHeight: number
  stepTop: number
}

export type AgentStepScrollDecision = {
  behavior: ScrollBehavior
  initializesRun: boolean
  top: number
}

/** Aligns the current step at the lower viewport edge with context below it. */
export function agentStepScrollTop({
  containerHeight,
  stepHeight,
  stepTop,
}: AgentStepScrollGeometry): number {
  return Math.max(stepTop - containerHeight + stepHeight + 18, 0)
}

/** Separates synchronous first positioning from later live auto-follow. */
export function agentStepScrollDecision({
  autoFollow,
  geometry,
  positionedRunId,
  reducedMotion,
  runId,
}: {
  autoFollow: boolean
  geometry: AgentStepScrollGeometry
  positionedRunId: string | null
  reducedMotion: boolean
  runId: string
}): AgentStepScrollDecision | null {
  const initializesRun = positionedRunId !== runId
  if (!initializesRun && !autoFollow) return null
  return {
    behavior: initializesRun || reducedMotion ? 'auto' : 'smooth',
    initializesRun,
    top: agentStepScrollTop(geometry),
  }
}

export function isAgentStepScrollKey(key: string): boolean {
  return [
    ' ',
    'ArrowDown',
    'ArrowUp',
    'End',
    'Home',
    'PageDown',
    'PageUp',
  ].includes(key)
}
