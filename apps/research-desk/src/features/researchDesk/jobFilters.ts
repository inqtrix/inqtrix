import type { JobFilter, ResearchJob } from './types'

/** The filter chips of the job list, in display order. A failed run has a
 * chip of its own, not only a place under "all". */
export const JOB_FILTERS: readonly JobFilter[] = [
  'all',
  'running',
  'queued',
  'cancelled',
  'failed',
  'completed',
]

/** Whether a job belongs under a filter. An unavailable run's card visibly
 * retracts its last status (calm lock), so it appears, and counts, only
 * under "all" until hydration prunes it. The job list and the selection
 * both ask here, a run record as well as its projected job. */
export function jobMatchesFilter(
  job: Pick<ResearchJob, 'status' | 'unavailable'>,
  filter: JobFilter,
): boolean {
  return filter === 'all' || (job.status === filter && !job.unavailable)
}

/** One chip per filter; its count is exactly the number of jobs it shows. */
export function jobFilterOptions(
  jobs: readonly ResearchJob[],
  labels: Record<JobFilter, string>,
): Array<{ count: number; key: JobFilter; label: string }> {
  return JOB_FILTERS.map((key) => ({
    count: jobs.filter((job) => jobMatchesFilter(job, key)).length,
    key,
    label: labels[key],
  }))
}
