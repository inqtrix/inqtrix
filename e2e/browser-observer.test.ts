import assert from 'node:assert/strict'
import { test } from 'node:test'

import {
  collaborationSocketWindow,
  installCollaborationWebSocketObserver,
  type CollaborationSocketObserverState,
} from './browser-observer.ts'

test('browser observer records one real durable ACK without content or unrelated sockets', () => {
  assert.doesNotThrow(() => Function(
    `return (${installCollaborationWebSocketObserver.toString()})`,
  )())

  class FakeWebSocket extends EventTarget {
    static readonly instances: FakeWebSocket[] = []
    readonly url: string

    constructor(url: string | URL, _protocols?: string | string[]) {
      super()
      this.url = String(url)
      FakeWebSocket.instances.push(this)
    }
  }

  const fakeWindow = {
    location: { href: 'https://app.example.test/editor' },
    WebSocket: FakeWebSocket,
  } as unknown as Window & typeof globalThis & {
    __inqtrixCollaborationSocketObserver?: CollaborationSocketObserverState
  }
  const originalWindow = Object.getOwnPropertyDescriptor(globalThis, 'window')
  Object.defineProperty(globalThis, 'window', {
    configurable: true,
    value: fakeWindow,
    writable: true,
  })

  try {
    installCollaborationWebSocketObserver()
    const ObservedWebSocket = fakeWindow.WebSocket as unknown as typeof FakeWebSocket
    const collaboration = new ObservedWebSocket('wss://app.example.test/collaboration')
    const vite = new ObservedWebSocket('wss://app.example.test/inqtrix')

    collaboration.dispatchEvent(new Event('open'))
    dispatchMessage(collaboration, durableAckFrame(0))
    dispatchMessage(collaboration, durableAckFrame(17))
    dispatchClose(collaboration, 1012)
    dispatchClose(vite, 1006)

    const observerState = fakeWindow.__inqtrixCollaborationSocketObserver!
    const durableAck = observerState.events.find((event) => (
      event.kind === 'durable_ack'
    ))
    assert.equal(typeof durableAck?.observedAt, 'number')
    assert.deepEqual({
      ...observerState,
      events: observerState.events.map((event) => (
        event.kind === 'durable_ack'
          ? { ...event, observedAt: '<measured>' }
          : event
      )),
    }, {
      events: [
        { kind: 'open', order: 1, socketId: 1 },
        { kind: 'protocol_error', order: 2, socketId: 1 },
        {
          kind: 'durable_ack',
          observedAt: '<measured>',
          order: 3,
          sequence: 17,
          socketId: 1,
        },
        { code: 1012, kind: 'close', order: 4, socketId: 1 },
      ],
      pendingFrameDecodes: 0,
    })
    assert.doesNotMatch(
      JSON.stringify(fakeWindow.__inqtrixCollaborationSocketObserver),
      /[a-f0-9]{64}/,
    )
    assert.deepEqual(
      collaborationSocketWindow(
        fakeWindow.__inqtrixCollaborationSocketObserver!.events,
        1,
        0,
        1012,
      )?.durableAcks,
      [{
        kind: 'durable_ack',
        observedAt: durableAck!.observedAt,
        order: 3,
        sequence: 17,
        socketId: 1,
      }],
    )
    assert.deepEqual(
      collaborationSocketWindow(
        fakeWindow.__inqtrixCollaborationSocketObserver!.events,
        1,
        0,
        1012,
      )?.protocolErrors,
      [{ kind: 'protocol_error', order: 2, socketId: 1 }],
    )
    assert.equal(
      collaborationSocketWindow(
        fakeWindow.__inqtrixCollaborationSocketObserver!.events,
        1,
        0,
      )?.close.code,
      1012,
    )
  } finally {
    if (originalWindow) Object.defineProperty(globalThis, 'window', originalWindow)
    else Reflect.deleteProperty(globalThis, 'window')
  }
})

function dispatchMessage(socket: EventTarget, bytes: Uint8Array): void {
  const event = new Event('message')
  Object.defineProperty(event, 'data', { value: bytes.buffer })
  socket.dispatchEvent(event)
}

function dispatchClose(socket: EventTarget, code: number): void {
  const event = new Event('close')
  Object.defineProperty(event, 'code', { value: code })
  socket.dispatchEvent(event)
}

function durableAckFrame(sequence: number): Uint8Array {
  return concat(
    encodeString('inqtrix-editor-v1:test:g1'),
    encodeVarUint(5),
    encodeString(JSON.stringify({
      hash: 'a'.repeat(64),
      sequence,
      type: 'durable_ack',
    })),
  )
}

function encodeString(value: string): Uint8Array {
  const bytes = new TextEncoder().encode(value)
  return concat(encodeVarUint(bytes.length), bytes)
}

function encodeVarUint(value: number): Uint8Array {
  const bytes: number[] = []
  let remaining = value
  do {
    let byte = remaining & 0x7f
    remaining = Math.floor(remaining / 128)
    if (remaining > 0) byte |= 0x80
    bytes.push(byte)
  } while (remaining > 0)
  return Uint8Array.from(bytes)
}

function concat(...parts: Uint8Array[]): Uint8Array {
  const result = new Uint8Array(parts.reduce((total, part) => total + part.length, 0))
  let offset = 0
  for (const part of parts) {
    result.set(part, offset)
    offset += part.length
  }
  return result
}
