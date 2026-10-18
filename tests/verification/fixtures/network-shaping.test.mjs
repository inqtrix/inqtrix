import assert from 'node:assert/strict'
import { test } from 'node:test'

import {
  PodmanNetworkShapingDriver,
  assertActiveNetworkProfile,
  assertDefaultQdisc,
  cleanupVerificationNetworkShape,
  netemArguments,
} from './network-shaping.mjs'

const COLLABORATION_CONTAINER = 'a'.repeat(64)
const WEB_CONTAINER = 'b'.repeat(64)
const NETWORK_ID = 'c'.repeat(64)
const WEB_IP = '10.89.0.13'
const API_IP = '10.89.0.11'

test('network shaping maps only the four approved netem profiles', () => {
  assert.deepEqual(netemArguments({ delayMs: 100, kind: 'delay' }), ['delay', '100ms'])
  assert.deepEqual(netemArguments({ delayMs: 300, kind: 'delay' }), ['delay', '300ms'])
  assert.deepEqual(netemArguments({ kind: 'rate', rate: '2mbit' }), ['rate', '2mbit'])
  assert.deepEqual(netemArguments({ kind: 'loss', percent: 1 }), ['loss', '1%'])
  assert.throws(() => netemArguments({ delayMs: 301, kind: 'delay' }), /not allowlisted/)
  assert.throws(() => netemArguments({ kind: 'loss', percent: 2 }), /not allowlisted/)
})

test('network shaping validates the effective netem profile without inherited options', () => {
  assert.doesNotThrow(() => assertActiveNetworkProfile(
    { delayMs: 100, kind: 'delay' },
    ownedQdisc('delay 100ms'),
  ))
  assert.doesNotThrow(() => assertActiveNetworkProfile(
    { kind: 'rate', rate: '2mbit' },
    ownedQdisc('rate 2Mbit'),
  ))
  assert.doesNotThrow(() => assertActiveNetworkProfile(
    { kind: 'loss', percent: 1 },
    ownedQdisc('loss 1%'),
  ))
  assert.throws(
    () => assertActiveNetworkProfile(
      { kind: 'loss', percent: 1 },
      ownedQdisc('loss 1% rate 2Mbit'),
    ),
    /does not match/,
  )
})

test('network shaping rejects a pre-existing foreign root qdisc', async () => {
  assert.doesNotThrow(() => assertDefaultQdisc('qdisc noqueue 0: root refcnt 2'))
  assert.throws(
    () => assertDefaultQdisc('qdisc fq_codel 0: root refcnt 2'),
    /foreign root qdisc/,
  )
  const driver = driverFor(fakeExecutor({
    initialQdisc: 'qdisc fq_codel 0: root refcnt 2',
  }))
  await assert.rejects(() => driver.initialize(), /foreign root qdisc/)
})

test('network shaping requires one exact common container network and peer IPv4', async () => {
  const missing = fakeExecutor({
    networks: {
      [COLLABORATION_CONTAINER]: networkMap('10.89.0.12', 'collaboration'),
      [WEB_CONTAINER]: networkMap(WEB_IP, 'web'),
    },
  })
  await assert.rejects(
    () => driverFor(missing).initialize(),
    /exactly one common network/,
  )

  const multiple = fakeExecutor({
    networks: {
      [COLLABORATION_CONTAINER]: {
        ...networkMap('10.89.0.12'),
        ...networkMap('10.90.0.12', 'second', 'd'.repeat(64)),
      },
      [WEB_CONTAINER]: {
        ...networkMap(WEB_IP),
        ...networkMap('10.90.0.13', 'second', 'd'.repeat(64)),
      },
    },
  })
  await assert.rejects(
    () => driverFor(multiple).initialize(),
    /exactly one common network/,
  )

  const mismatchedId = fakeExecutor({
    networks: {
      [COLLABORATION_CONTAINER]: networkMap('10.89.0.12'),
      [WEB_CONTAINER]: networkMap(WEB_IP, 'inqtrix_default', 'd'.repeat(64)),
    },
  })
  await assert.rejects(
    () => driverFor(mismatchedId).initialize(),
    /network identity mismatch/,
  )

  const invalidPeer = fakeExecutor({
    networks: {
      [COLLABORATION_CONTAINER]: networkMap('10.89.0.12'),
      [WEB_CONTAINER]: networkMap(API_IP),
    },
  })
  invalidPeer.networks[WEB_CONTAINER].inqtrix_default.IPAddress = 'not-an-ip'
  await assert.rejects(
    () => driverFor(invalidPeer).initialize(),
    /valid distinct IPv4/,
  )
})

test('network shaping classifies only collaboration egress to the canonical web peer', async () => {
  const fake = fakeExecutor()
  const driver = driverFor(fake)

  assert.deepEqual(await driver.initialize(), {
    interface: 'eth0',
    peerIPv4: WEB_IP,
    state: 'default',
  })
  assert.deepEqual(await driver.apply('latency-100ms'), {
    phaseId: 'latency-100ms',
    state: 'netem',
  })
  assert.match(fake.state().qdisc, /qdisc prio 8001: root/)
  assert.match(fake.state().qdisc, /qdisc netem 8030: parent 8001:3.*delay 100ms/)
  assert.match(fake.state().filter, new RegExp(`dst_ip ${WEB_IP.replaceAll('.', '\\.')}`))
  assert.match(fake.state().filter, /classid 8001:3/)

  const commands = fake.calls.map(({ args }) => args.join(' ')).join('\n')
  assert.match(commands, new RegExp(`flower dst_ip ${WEB_IP.replaceAll('.', '\\.')}\\/32 classid 8001:3`))
  assert.doesNotMatch(commands, new RegExp(API_IP.replaceAll('.', '\\.')))

  assert.deepEqual(await driver.apply('packet-loss-1pct'), {
    phaseId: 'packet-loss-1pct',
    state: 'netem',
  })
  assert.match(fake.state().qdisc, /netem.*loss 1%/)
  assert.deepEqual(await driver.apply('normalized'), {
    phaseId: 'normalized',
    state: 'default',
  })
  assert.match(fake.state().qdisc, /noqueue/)
  assert.equal(fake.state().filter, '')
  await driver.close()
  assert.match(fake.state().qdisc, /noqueue/)
  assert(fake.calls.every(({ command }) => command === 'podman'))
  assert(fake.calls.every(({ args }) => Array.isArray(args) && !args.includes('sh')))
})

test('network shaping rebuilds state between rate and loss profiles', async () => {
  const fake = fakeExecutor()
  const driver = driverFor(fake)
  await driver.initialize()
  await driver.apply('bandwidth-2mbit')

  const transitionStart = fake.calls.length
  await driver.apply('packet-loss-1pct')
  const transition = fake.calls.slice(transitionStart).map(({ args }) => args)

  assert(
    transition.some((args) => args.includes('qdisc') && args.includes('del') && args.includes('root')),
    'rate-to-loss transition must remove the previous verification-owned qdisc',
  )
  assert(
    transition.some((args) => args.includes('qdisc') && args.includes('add') && args.includes('root')),
    'rate-to-loss transition must install a fresh verification-owned qdisc',
  )
  assert(
    !transition.some((args) => args.includes('qdisc') && args.includes('replace')),
    'rate-to-loss transition must not retain unspecified netem options',
  )
  assert.match(fake.state().qdisc, /netem.*loss 1%/)
  assert.doesNotMatch(fake.state().qdisc, /rate 2mbit/i)

  await driver.close()
})

test('network shaping cleanup removes its partial classful qdisc after an interrupted transition', async () => {
  const fake = fakeExecutor({ failNetem: true })
  const driver = driverFor(fake)
  await driver.initialize()
  await assert.rejects(() => driver.apply('latency-300ms'), /synthetic netem failure/)
  assert.match(fake.state().qdisc, /qdisc prio 8001: root/)
  await driver.close()
  assert.match(fake.state().qdisc, /noqueue/)
})

test('central cleanup re-resolves both peers and removes only the owned filtered qdisc', async () => {
  const fake = fakeExecutor({
    initialFilter: ownedFilter(),
    initialQdisc: ownedQdisc('delay 100ms'),
  })
  assert.deepEqual(await cleanupVerificationNetworkShape({
    containerId: COLLABORATION_CONTAINER,
    execute: fake.execute,
    peerContainerId: WEB_CONTAINER,
    repositoryRoot: '/repo',
  }), { interface: 'eth0', state: 'default' })
  assert.match(fake.state().qdisc, /noqueue/)
  assert.equal(fake.state().filter, '')
  assert.equal(fake.calls[0]?.args[0], 'inspect')
  assert(fake.calls.some(({ args }) => args.includes('del')))
})

test('central cleanup is idempotent for noqueue and refuses foreign or misrouted qdisc', async () => {
  const clean = fakeExecutor()
  await cleanupVerificationNetworkShape({
    containerId: COLLABORATION_CONTAINER,
    execute: clean.execute,
    peerContainerId: WEB_CONTAINER,
    repositoryRoot: '/repo',
  })
  assert(!clean.calls.some(({ args }) => args.includes('del')))

  const foreign = fakeExecutor({ initialQdisc: 'qdisc fq_codel 0: root refcnt 2' })
  await assert.rejects(
    () => cleanupVerificationNetworkShape({
      containerId: COLLABORATION_CONTAINER,
      execute: foreign.execute,
      peerContainerId: WEB_CONTAINER,
      repositoryRoot: '/repo',
    }),
    /refuses to remove a foreign root qdisc/,
  )
  assert.match(foreign.state().qdisc, /fq_codel/)
  assert(!foreign.calls.some(({ args }) => args.includes('del')))

  const misrouted = fakeExecutor({
    initialFilter: ownedFilter(API_IP),
    initialQdisc: ownedQdisc('loss 1%'),
  })
  await assert.rejects(
    () => cleanupVerificationNetworkShape({
      containerId: COLLABORATION_CONTAINER,
      execute: misrouted.execute,
      peerContainerId: WEB_CONTAINER,
      repositoryRoot: '/repo',
    }),
    /peer filter/,
  )
  assert.match(misrouted.state().qdisc, /qdisc prio 8001: root/)
  assert(!misrouted.calls.some(({ args }) => args.includes('del')))
})

function driverFor(fake) {
  return new PodmanNetworkShapingDriver({
    containerId: COLLABORATION_CONTAINER,
    execute: fake.execute,
    peerContainerId: WEB_CONTAINER,
    repositoryRoot: '/repo',
  })
}

function networkMap(ip, name = 'inqtrix_default', networkId = NETWORK_ID) {
  return {
    [name]: {
      IPAddress: ip,
      IPPrefixLen: 24,
      NetworkID: networkId,
    },
  }
}

function ownedQdisc(netem) {
  return [
    'qdisc prio 8001: root refcnt 2 bands 3 priomap 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0',
    `qdisc netem 8030: parent 8001:3 limit 1000 ${netem}`,
  ].join('\n')
}

function ownedFilter(ip = WEB_IP) {
  return [
    'filter protocol ip pref 1 flower chain 0',
    `filter protocol ip pref 1 flower chain 0 handle 0x1 dst_ip ${ip} classid 8001:3`,
  ].join('\n')
}

function fakeExecutor({
  failNetem = false,
  initialFilter = '',
  initialQdisc = 'qdisc noqueue 0: root refcnt 2',
  networks = {
    [COLLABORATION_CONTAINER]: networkMap('10.89.0.12'),
    [WEB_CONTAINER]: networkMap(WEB_IP),
  },
} = {}) {
  let filter = initialFilter
  let qdisc = initialQdisc
  const calls = []
  const result = {
    calls,
    networks,
    async execute(command, args) {
      calls.push({ args: [...args], command })
      if (args[0] === 'inspect') {
        if (args[2] === '{{.State.Pid}}') return { stdout: '4242\n' }
        if (args[2] === '{{json .NetworkSettings.Networks}}') {
          return { stdout: `${JSON.stringify(networks[args[3]])}\n` }
        }
      }
      const tcIndex = args.indexOf('tc')
      const tc = args.slice(tcIndex)
      if (tc[1] === 'qdisc' && tc.includes('show')) return { stdout: `${qdisc}\n` }
      if (tc[1] === 'filter' && tc.includes('show')) return { stdout: `${filter}\n` }
      if (tc[1] === 'qdisc' && tc.includes('root') && tc.includes('prio')) {
        qdisc = ownedQdisc('').split('\n')[0]
        return { stdout: '' }
      }
      if (tc[1] === 'qdisc' && tc.includes('parent') && tc.includes('netem')) {
        const netem = tc.slice(tc.indexOf('netem') + 1).join(' ')
        if (failNetem) throw new Error('synthetic netem failure')
        qdisc = ownedQdisc(netem)
        return { stdout: '' }
      }
      if (tc[1] === 'filter' && tc.includes('flower')) {
        const peer = tc[tc.indexOf('dst_ip') + 1].replace('/32', '')
        filter = ownedFilter(peer)
        return { stdout: '' }
      }
      if (tc[1] === 'qdisc' && tc.includes('del')) {
        filter = ''
        qdisc = 'qdisc noqueue 0: root refcnt 2'
        return { stdout: '' }
      }
      throw new Error(`Unexpected command: ${args.join(' ')}`)
    },
    state: () => ({ filter, qdisc }),
  }
  return result
}
