export type FaultControlContainers = {
  collaboration: string
  project: string
  web: string
}

export function resolveFaultControlContainers(options: {
  engine: string
  repositoryRoot: string
}): Promise<FaultControlContainers>
