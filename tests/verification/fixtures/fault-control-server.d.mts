export type FaultControlContainers = {
  collaboration: string
  project: string
  web: string
}

export function resolveFaultControlContainers(options: {
  engine: string
  repositoryRoot: string
}): Promise<FaultControlContainers>

export type ServiceContainer = { id: string; running: boolean; service: string }

export function listServiceContainers(options: {
  engine: string | null
  repositoryRoot: string
  services: readonly string[]
}): Promise<ServiceContainer[]>
