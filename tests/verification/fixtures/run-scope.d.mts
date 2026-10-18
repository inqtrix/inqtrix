export const VERIFICATION_RUN_ID_PATTERN: RegExp

export function assertVerificationRunId(
  value: unknown,
  label?: string,
): string

export function documentBelongsToRun(
  documentId: unknown,
  runId: string,
): boolean

export function agentSessionTitleForRun(runId: string): string

export function agentSessionBelongsToRun(
  title: unknown,
  runId: string,
): boolean

export function temporaryUserDescriptors(runId: string, count?: number): Array<{
  displayName: string
  email: string
}>

export function temporaryUserBelongsToRun(
  email: unknown,
  runId: string,
): boolean
