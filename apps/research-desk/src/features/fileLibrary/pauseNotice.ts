import type { AutoResumePlan } from './indexingTypes'

/** What an indexing pause waits for, read from the server's error type. */
export type PauseCause =
  | 'contextualization'
  | 'circuitOpen'
  | 'embedding'
  | 'vectorStore'
  | 'quota'
  | 'unknown'

export type AutoResumeNotice =
  | { kind: 'planned'; at: Date; attempt: number; max: number }
  | { kind: 'soon'; attempt: number; max: number }
  | {
      kind: 'stopped'
      reason: NonNullable<AutoResumePlan['stopped']>
      attempts: number
      max: number
    }

export function pauseCause(errorType: string | null | undefined): PauseCause {
  const type = errorType ?? ''
  if (type === 'contextualization_provider_circuit_open') return 'circuitOpen'
  if (type.startsWith('contextualization_provider_')) return 'contextualization'
  if (type.startsWith('embedding_provider_')) return 'embedding'
  if (type.startsWith('vector_store_')) return 'vectorStore'
  if (type.startsWith('quota_')) return 'quota'
  return 'unknown'
}

/** A build without retrieval context helps only where context is what failed. */
export function allowsRawRebuild(
  status: string | undefined,
  errorType: string | null | undefined,
): boolean {
  return status === 'paused_validation'
    || (errorType ?? '').startsWith('contextualization_')
}

/** The line a dependency pause shows about its automatic resume, if any. */
export function autoResumeNotice(
  status: string | undefined,
  plan: AutoResumePlan | null | undefined,
  nowMs: number,
): AutoResumeNotice | null {
  if (status !== 'paused_dependency' || !plan) return null
  if (plan.stopped) {
    return { attempts: plan.attempts, kind: 'stopped', max: plan.max, reason: plan.stopped }
  }
  if (plan.next_at == null) return null
  const attempt = plan.attempts + 1
  return plan.next_at * 1000 <= nowMs
    ? { attempt, kind: 'soon', max: plan.max }
    : { at: new Date(plan.next_at * 1000), attempt, kind: 'planned', max: plan.max }
}

/** The plan a paused run of several jobs hears from first. */
export function soonestPlan(
  plans: ReadonlyArray<AutoResumePlan | null | undefined>,
): AutoResumePlan | null {
  let soonest: AutoResumePlan | null = null
  for (const plan of plans) {
    if (!plan || plan.stopped || plan.next_at == null) continue
    if (!soonest || plan.next_at < (soonest.next_at ?? Infinity)) soonest = plan
  }
  return soonest ?? plans.find((plan) => plan?.stopped) ?? null
}
