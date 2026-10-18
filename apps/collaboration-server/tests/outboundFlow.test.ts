import {
  OutboundFlowController,
  type OutboundSocket,
} from '../src/outboundFlow'

const OPEN = 1

describe('outbound WebSocket flow control', () => {
  it('keeps frames charged until send completion and rejects a receiver that never drains', () => {
    const controller = controllerWithLimits({ maximumFrames: 2 })
    const socket = fakeSocket(false)

    send(controller, socket, 'slow', new Uint8Array(8))
    send(controller, socket, 'slow', new Uint8Array(8))

    expect(controller.reserve('slow', socket, 1)).toBeNull()
    expect(socket.callbacks).toHaveLength(2)
    socket.callbacks.shift()?.()
    expect(controller.reserve('slow', socket, 1)).not.toBeNull()
  })

  it('checks projected bufferedAmount again when a held reservation is flushed', () => {
    const controller = controllerWithLimits({ maximumBufferedBytes: 16 })
    const socket = fakeSocket(false)
    const held = controller.reserve('held', socket, 8)
    if (!held) throw new Error('held reservation was unexpectedly rejected')

    socket.bufferedAmount = 9

    expect(controller.send(held, socket, new Uint8Array(8), () => undefined)).toBe(false)
    expect(socket.callbacks).toHaveLength(0)
    socket.bufferedAmount = 0
    expect(controller.reserve('held', socket, 16)).not.toBeNull()
  })

  it('accounts concurrent receivers independently and releases successful broadcasts', () => {
    const samples: Array<{ bytes: number; frames: number }> = []
    const controller = controllerWithLimits({
      maximumFrames: 1,
      onUsageChange: (bytes, frames) => samples.push({ bytes, frames }),
    })
    const slow = fakeSocket(false)
    const draining = fakeSocket(true)

    send(controller, slow, 'slow', new Uint8Array(4))
    send(controller, draining, 'draining', new Uint8Array(4))
    expect(controller.reserve('slow', slow, 1)).toBeNull()
    expect(controller.reserve('draining', draining, 4)).not.toBeNull()
    expect(samples).toContainEqual({ bytes: 4, frames: 1 })
  })
})

type TestPayload = Uint8Array

type FakeSocket = OutboundSocket<TestPayload> & {
  callbacks: Array<(error?: Error) => void>
}

function controllerWithLimits(options: {
  maximumBufferedBytes?: number
  maximumBytes?: number
  maximumFrames?: number
  onUsageChange?: (bytes: number, frames: number) => void
} = {}): OutboundFlowController<TestPayload> {
  return new OutboundFlowController(
    options.maximumFrames ?? 8,
    options.maximumBytes ?? 1024,
    options.maximumBufferedBytes ?? 1024,
    OPEN,
    options.onUsageChange ?? (() => undefined),
  )
}

function fakeSocket(drainImmediately: boolean): FakeSocket {
  const callbacks: Array<(error?: Error) => void> = []
  return {
    bufferedAmount: 0,
    callbacks,
    readyState: OPEN,
    send: (_payload, callback) => {
      if (drainImmediately) callback()
      else callbacks.push(callback)
    },
  }
}

function send(
  controller: OutboundFlowController<TestPayload>,
  socket: FakeSocket,
  transportId: string,
  payload: TestPayload,
): void {
  const reservation = controller.reserve(transportId, socket, payload.byteLength)
  if (!reservation) throw new Error('outbound reservation was unexpectedly rejected')
  expect(controller.send(reservation, socket, payload, () => undefined)).toBe(true)
}
