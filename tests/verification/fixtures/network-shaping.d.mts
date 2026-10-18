export type ContainerCommandResult = {
  stdout?: string
}

export type ContainerCommandExecutor = (
  command: string,
  args: string[],
  cwd: string,
  operation: string,
) => Promise<ContainerCommandResult>

export class PodmanNetworkShapingDriver {
  constructor(options: {
    containerId: string
    execute?: ContainerCommandExecutor
    peerContainerId: string
    repositoryRoot: string
  })
  apply(phaseId: string): Promise<{ phaseId: string; state: 'default' | 'netem' }>
  close(): Promise<void>
  initialize(): Promise<{ interface: string; state: 'default' }>
}

export function cleanupVerificationNetworkShape(options: {
  containerId: string
  execute?: ContainerCommandExecutor
  peerContainerId: string
  repositoryRoot: string
}): Promise<{ interface: string; state: 'default' }>

export function assertDefaultQdisc(value: unknown): void
export function assertVerificationOwnedQdisc(value: unknown): void
export function netemArguments(network: unknown): string[]
