import { isIP } from 'node:net'

import { SOAK_NETWORK_PHASES } from '../../load/collaboration-load-lib.mjs'

import { requireContainerControlCommand } from './container-control.mjs'

const INTERFACE = 'eth0'
const ROOT_HANDLE = '8001:'
const SHAPED_CLASS = '8001:3'
const NETEM_HANDLE = '8030:'
const CONTAINER_ID = /^[a-f0-9]{12,64}$/i
const NETWORKS = new Map(
  SOAK_NETWORK_PHASES.map((phase) => [phase.id, phase.network]),
)
const UNCLASSIFIED_PRIOMAP = Array.from({ length: 16 }, () => '0')

export class PodmanNetworkShapingDriver {
  #containerId
  #execute
  #initialized = false
  #owned = false
  #peerContainerId
  #peerIPv4 = null
  #pid = null
  #repositoryRoot

  constructor({
    containerId,
    execute = requireContainerControlCommand,
    peerContainerId,
    repositoryRoot,
  }) {
    assertTargets({ containerId, peerContainerId, repositoryRoot })
    this.#containerId = containerId
    this.#execute = execute
    this.#peerContainerId = peerContainerId
    this.#repositoryRoot = repositoryRoot
  }

  async initialize() {
    if (this.#initialized) throw new Error('Network shaping is already initialized.')
    this.#pid = await resolveContainerPid({
      containerId: this.#containerId,
      execute: this.#execute,
      repositoryRoot: this.#repositoryRoot,
    })
    this.#peerIPv4 = await resolveCommonPeerIPv4({
      containerId: this.#containerId,
      execute: this.#execute,
      peerContainerId: this.#peerContainerId,
      repositoryRoot: this.#repositoryRoot,
    })
    const current = await this.#showQdisc()
    assertDefaultQdisc(current)
    this.#initialized = true
    return { interface: INTERFACE, peerIPv4: this.#peerIPv4, state: 'default' }
  }

  async apply(phaseId) {
    if (!this.#initialized || !this.#pid || !this.#peerIPv4) {
      throw new Error('Network shaping must be initialized before applying a phase.')
    }
    const network = NETWORKS.get(phaseId)
    if (!network) throw new Error('Network shaping phase is not allowlisted.')
    if (network.kind === 'normal') {
      await this.#normalize()
      return { phaseId, state: 'default' }
    }

    const netem = netemArguments(network)
    if (this.#owned) {
      await this.#normalize()
    } else {
      assertDefaultQdisc(await this.#showQdisc())
    }
    await this.#executeTc(
      [
        'qdisc', 'add', 'dev', INTERFACE, 'root', 'handle', ROOT_HANDLE,
        'prio', 'bands', '3', 'priomap', ...UNCLASSIFIED_PRIOMAP,
      ],
      `Installing the verification network classifier for ${phaseId}`,
    )
    this.#owned = true
    await this.#executeTc(
      [
        'qdisc', 'add', 'dev', INTERFACE, 'parent', SHAPED_CLASS,
        'handle', NETEM_HANDLE, 'netem', ...netem,
      ],
      `Applying allowlisted network phase ${phaseId}`,
    )
    await this.#executeTc(
      [
        'filter', 'replace', 'dev', INTERFACE, 'protocol', 'ip',
        'parent', ROOT_HANDLE, 'prio', '1', 'flower',
        'dst_ip', `${this.#peerIPv4}/32`, 'classid', SHAPED_CLASS,
      ],
      `Classifying canonical web traffic for ${phaseId}`,
    )

    const qdisc = await this.#showQdisc()
    assertVerificationOwnedState({
      filter: await this.#showFilter(),
      peerIPv4: this.#peerIPv4,
      qdisc,
    })
    assertActiveNetworkProfile(network, qdisc)
    return { phaseId, state: 'netem' }
  }

  async close() {
    if (!this.#initialized || !this.#pid) return
    await this.#normalize()
    this.#initialized = false
    this.#peerIPv4 = null
    this.#pid = null
  }

  async #normalize() {
    if (this.#owned) {
      const qdisc = await this.#showQdisc()
      const filter = hasOwnedRoot(qdisc) ? await this.#showFilter() : ''
      assertVerificationOwnedState({
        allowPartial: true,
        filter,
        peerIPv4: this.#peerIPv4,
        qdisc,
      })
      await this.#executeTc(
        ['qdisc', 'del', 'dev', INTERFACE, 'root'],
        'Removing the verification-owned network qdisc',
      )
      this.#owned = false
    }
    assertDefaultQdisc(await this.#showQdisc())
  }

  async #executeTc(command, operation) {
    return await this.#execute(
      'podman',
      this.#machineCommand(['tc', ...command]),
      this.#repositoryRoot,
      operation,
    )
  }

  async #showFilter() {
    return await showFilter({
      execute: this.#execute,
      pid: this.#pid,
      repositoryRoot: this.#repositoryRoot,
    })
  }

  async #showQdisc() {
    return await showQdisc({
      execute: this.#execute,
      pid: this.#pid,
      repositoryRoot: this.#repositoryRoot,
    })
  }

  #machineCommand(command) {
    return machineCommand(this.#pid, command)
  }
}

export async function cleanupVerificationNetworkShape({
  containerId,
  execute = requireContainerControlCommand,
  peerContainerId,
  repositoryRoot,
}) {
  assertTargets({ containerId, peerContainerId, repositoryRoot })
  const pid = await resolveContainerPid({ containerId, execute, repositoryRoot })
  const peerIPv4 = await resolveCommonPeerIPv4({
    containerId,
    execute,
    peerContainerId,
    repositoryRoot,
  })
  const qdisc = await showQdisc({ execute, pid, repositoryRoot })
  if (isDefaultQdisc(qdisc)) {
    return { interface: INTERFACE, state: 'default' }
  }
  assertVerificationOwnedState({
    allowPartial: true,
    filter: await showFilter({ execute, pid, repositoryRoot }),
    peerIPv4,
    qdisc,
  })
  await execute(
    'podman',
    machineCommand(pid, ['tc', 'qdisc', 'del', 'dev', INTERFACE, 'root']),
    repositoryRoot,
    'Removing the centrally registered verification network qdisc',
  )
  assertDefaultQdisc(await showQdisc({ execute, pid, repositoryRoot }))
  return { interface: INTERFACE, state: 'default' }
}

export function assertDefaultQdisc(value) {
  if (isDefaultQdisc(value)) return
  throw new Error('Network shaping refuses to replace a foreign root qdisc.')
}

export function assertVerificationOwnedQdisc(value) {
  assertOwnedQdisc(value, false)
}

export function netemArguments(network) {
  if (network?.kind === 'delay' && [100, 300].includes(network.delayMs)) {
    return ['delay', `${network.delayMs}ms`]
  }
  if (network?.kind === 'rate' && network.rate === '2mbit') {
    return ['rate', network.rate]
  }
  if (network?.kind === 'loss' && network.percent === 1) {
    return ['loss', '1%']
  }
  throw new Error('Network shaping parameters are not allowlisted.')
}

export function assertActiveNetworkProfile(network, qdisc) {
  const line = qdiscLines(qdisc).find(isOwnedNetem) ?? ''
  const hasDelay = /\bdelay\s+\S+/i.test(line)
  const hasLoss = /\bloss\s+\S+/i.test(line)
  const hasRate = /\brate\s+\S+/i.test(line)
  const delayMatches = network?.kind === 'delay'
    && new RegExp(`\\bdelay\\s+${network.delayMs}ms\\b`, 'i').test(line)
    && !hasLoss
    && !hasRate
  const rateMatches = network?.kind === 'rate'
    && /\brate\s+2mbit\b/i.test(line)
    && !hasDelay
    && !hasLoss
  const lossMatches = network?.kind === 'loss'
    && /\bloss(?:\s+random)?\s+1(?:\.0+)?%(?=\s|$)/i.test(line)
    && !hasDelay
    && !hasRate
  if (delayMatches || rateMatches || lossMatches) return
  throw new Error('Active network qdisc does not match the allowlisted phase profile.')
}

function assertTargets({ containerId, peerContainerId, repositoryRoot }) {
  if (typeof containerId !== 'string' || !CONTAINER_ID.test(containerId)) {
    throw new Error('Network shaping requires an exact container identifier.')
  }
  if (typeof peerContainerId !== 'string' || !CONTAINER_ID.test(peerContainerId)) {
    throw new Error('Network shaping requires an exact peer container identifier.')
  }
  if (containerId === peerContainerId) {
    throw new Error('Network shaping requires distinct target and peer containers.')
  }
  if (typeof repositoryRoot !== 'string' || repositoryRoot.length === 0) {
    throw new Error('Network shaping requires the repository root.')
  }
}

function assertVerificationOwnedState({
  allowPartial = false,
  filter,
  peerIPv4,
  qdisc,
}) {
  assertOwnedQdisc(qdisc, allowPartial)
  const trimmedFilter = String(filter ?? '').trim()
  const hasChild = qdiscLines(qdisc).some(isOwnedNetem)
  if (allowPartial && trimmedFilter.length === 0) return
  if (!hasChild || trimmedFilter.length === 0) {
    throw new Error('Network shaping cleanup refuses to remove a qdisc without its peer filter.')
  }
  assertOwnedPeerFilter(trimmedFilter, peerIPv4)
}

function assertOwnedQdisc(value, allowPartial) {
  const lines = qdiscLines(value)
  const root = lines.filter(isOwnedRoot)
  const child = lines.filter(isOwnedNetem)
  if (
    root.length === 1
      && (child.length === 1 || (allowPartial && child.length === 0))
      && root.length + child.length === lines.length
  ) return
  throw new Error('Network shaping cleanup refuses to remove a foreign root qdisc.')
}

function assertOwnedPeerFilter(value, peerIPv4) {
  const filter = String(value).trim()
  const destinations = [...filter.matchAll(/\bdst_ip\s+([0-9.]+)(?:\/32)?\b/g)]
    .map((match) => match[1])
  const classes = [...filter.matchAll(/\bclassid\s+(\S+)/g)]
    .map((match) => match[1])
  if (
    !/\bflower\b/.test(filter)
      || destinations.length !== 1
      || destinations[0] !== peerIPv4
      || classes.length !== 1
      || classes[0] !== SHAPED_CLASS
  ) {
    throw new Error(
      'Network shaping cleanup refuses to remove a qdisc without the exact canonical web peer filter.',
    )
  }
}

async function resolveContainerPid({ containerId, execute, repositoryRoot }) {
  const inspected = await execute(
    'podman',
    ['inspect', '--format', '{{.State.Pid}}', containerId],
    repositoryRoot,
    'Resolving the collaboration network namespace',
  )
  const pid = String(inspected.stdout ?? '').trim()
  if (!/^[1-9][0-9]{1,9}$/.test(pid)) {
    throw new Error('Podman returned an invalid collaboration process identifier.')
  }
  return pid
}

async function resolveCommonPeerIPv4({
  containerId,
  execute,
  peerContainerId,
  repositoryRoot,
}) {
  const [targetNetworks, peerNetworks] = await Promise.all([
    inspectNetworks({ containerId, execute, repositoryRoot }),
    inspectNetworks({ containerId: peerContainerId, execute, repositoryRoot }),
  ])
  const common = Object.keys(targetNetworks)
    .filter((networkName) => Object.hasOwn(peerNetworks, networkName))
  if (common.length !== 1) {
    throw new Error('Network shaping requires exactly one common network between target and peer.')
  }
  const networkName = common[0]
  const target = targetNetworks[networkName]
  const peer = peerNetworks[networkName]
  if (
    typeof target?.NetworkID !== 'string'
      || !CONTAINER_ID.test(target.NetworkID)
      || target.NetworkID !== peer?.NetworkID
  ) {
    throw new Error('Network shaping detected a container network identity mismatch.')
  }
  if (
    isIP(target?.IPAddress) !== 4
      || isIP(peer?.IPAddress) !== 4
      || target.IPAddress === peer.IPAddress
  ) {
    throw new Error('Network shaping requires a valid distinct IPv4 address for each peer.')
  }
  return peer.IPAddress
}

async function inspectNetworks({ containerId, execute, repositoryRoot }) {
  const inspected = await execute(
    'podman',
    ['inspect', '--format', '{{json .NetworkSettings.Networks}}', containerId],
    repositoryRoot,
    'Resolving the canonical container network attachment',
  )
  let networks
  try {
    networks = JSON.parse(String(inspected.stdout ?? '').trim())
  } catch {
    throw new Error('Podman returned invalid container network metadata.')
  }
  if (!networks || typeof networks !== 'object' || Array.isArray(networks)) {
    throw new Error('Podman returned invalid container network metadata.')
  }
  return networks
}

async function showFilter({ execute, pid, repositoryRoot }) {
  const result = await execute(
    'podman',
    machineCommand(pid, [
      'tc', 'filter', 'show', 'dev', INTERFACE, 'protocol', 'ip', 'parent', ROOT_HANDLE,
    ]),
    repositoryRoot,
    'Inspecting the collaboration network peer filter',
  )
  return String(result.stdout ?? '').trim()
}

async function showQdisc({ execute, pid, repositoryRoot }) {
  const result = await execute(
    'podman',
    machineCommand(pid, ['tc', 'qdisc', 'show', 'dev', INTERFACE]),
    repositoryRoot,
    'Inspecting the collaboration network qdisc',
  )
  return String(result.stdout ?? '').trim()
}

function machineCommand(pid, command) {
  return [
    'machine',
    'ssh',
    'sudo',
    'nsenter',
    '-t',
    pid,
    '-n',
    ...command,
  ]
}

function hasOwnedRoot(value) {
  return qdiscLines(value).some(isOwnedRoot)
}

function isDefaultQdisc(value) {
  const lines = qdiscLines(value)
  return lines.length === 0 || lines.every((line) => /^qdisc noqueue\b/.test(line))
}

function isOwnedNetem(line) {
  return /^qdisc netem 8030:\s+parent 8001:3\b/.test(line)
}

function isOwnedRoot(line) {
  if (!/^qdisc prio 8001:\s+root\b/.test(line)) return false
  const match = line.match(/\bpriomap\s+((?:[0-9]+\s*)+)$/)
  if (!match) return false
  return match[1].trim().split(/\s+/).length === 16
    && match[1].trim().split(/\s+/).every((entry) => entry === '0')
    && /\bbands\s+3\b/.test(line)
}

function qdiscLines(value) {
  return String(value).trim().split('\n').map((line) => line.trim()).filter(Boolean)
}
