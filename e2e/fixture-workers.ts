const WORKER_MEMORY_BYTES = 6 * 1024 ** 3

/**
 * Browser workers for the dev-server fixtures. One worker holds browsers with
 * several writers that each parse hundreds of unbundled modules. More workers than
 * the memory carries send the machine into swap, and then unrelated tests time out
 * together, so memory bounds the usual half of the cores.
 */
export function fixtureWorkers(totalMemoryBytes: number, cpuCount: number): number {
  return Math.max(1, Math.min(
    Math.floor(cpuCount / 2),
    Math.floor(totalMemoryBytes / WORKER_MEMORY_BYTES),
  ))
}
