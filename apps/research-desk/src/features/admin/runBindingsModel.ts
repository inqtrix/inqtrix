import type { AdminRunBindings } from '@/api/inqtrixClient'

/** How the server counts a running run whose cancel is already requested. */
const ASKED_TO_STOP = 'cancel_requested'

/** One count row: open runs of one status bound to one contract. */
export type RunBindingRow = {
  status: string
  contract: number
  runs: number
  /** No worker of this server's version runs these. */
  outside: boolean
  /** Running runs already asked to stop: the cancel neither offers nor asks them again. */
  asked: boolean
}

/** One mode's open runs, judged against the contract this server carries. */
export type RunBindingGroup = {
  mode: string
  /** The contract this server carries for the mode; `null` when it does not register it. */
  serverContract: number | null
  rows: RunBindingRow[]
  /** The runs an admin cancel of this mode ends (those already asked to stop are not counted). */
  outsideRuns: number
}

/**
 * The open runs per mode, judged as the admin cancel judges them.
 *
 * A run bound above the server's contract, or of a mode the server does not
 * register, is one no worker of this version runs; a run bound below ends at
 * its next claim on its own and is not counted as outside.
 */
export function runBindingGroups(bindings: AdminRunBindings): RunBindingGroup[] {
  const groups = new Map<string, RunBindingGroup>()
  for (const count of bindings.counts) {
    const serverContract = Object.hasOwn(bindings.contracts, count.mode)
      ? bindings.contracts[count.mode]
      : null
    const group = groups.get(count.mode)
      ?? { mode: count.mode, serverContract, rows: [], outsideRuns: 0 }
    const outside = serverContract == null || count.workflow_contract > serverContract
    const asked = count.status === ASKED_TO_STOP
    group.rows.push({
      status: count.status,
      contract: count.workflow_contract,
      runs: count.runs,
      outside,
      asked,
    })
    if (outside && !asked) group.outsideRuns += count.runs
    groups.set(count.mode, group)
  }
  return [...groups.values()]
    .map((group) => ({
      ...group,
      rows: [...group.rows].sort(
        (a, b) => a.contract - b.contract || a.status.localeCompare(b.status),
      ),
    }))
    .sort((a, b) => a.mode.localeCompare(b.mode))
}

/** The contracts this server carries, by mode name. */
export function serverContracts(bindings: AdminRunBindings): Array<[string, number]> {
  return Object.entries(bindings.contracts).sort(([a], [b]) => a.localeCompare(b))
}
