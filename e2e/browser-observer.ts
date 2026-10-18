export type CollaborationSocketEvent =
  | { kind: 'close'; code: number; order: number; socketId: number }
  | {
      kind: 'durable_ack'
      observedAt: number
      order: number
      sequence: number
      socketId: number
    }
  | { kind: 'open'; order: number; socketId: number }
  | { kind: 'protocol_error'; order: number; socketId: number }

export type CollaborationSocketObserverState = {
  events: CollaborationSocketEvent[]
  pendingFrameDecodes: number
}

export function collaborationSocketWindow(
  events: CollaborationSocketEvent[],
  socketId: number,
  afterOrder: number,
  closeCode?: number,
): {
  close: Extract<CollaborationSocketEvent, { kind: 'close' }>
  durableAcks: Array<Extract<CollaborationSocketEvent, { kind: 'durable_ack' }>>
  protocolErrors: Array<Extract<CollaborationSocketEvent, { kind: 'protocol_error' }>>
} | null {
  const close = events.find((event): event is Extract<
    CollaborationSocketEvent,
    { kind: 'close' }
  > => (
    event.kind === 'close'
    && event.socketId === socketId
    && event.order > afterOrder
    && (closeCode === undefined || event.code === closeCode)
  ))
  if (!close) return null
  return {
    close,
    durableAcks: events.filter((event): event is Extract<
      CollaborationSocketEvent,
      { kind: 'durable_ack' }
    > => (
      event.kind === 'durable_ack'
      && event.socketId === socketId
      && event.order > afterOrder
      && event.order < close.order
    )),
    protocolErrors: events.filter((event): event is Extract<
      CollaborationSocketEvent,
      { kind: 'protocol_error' }
    > => (
      event.kind === 'protocol_error'
      && event.socketId === socketId
      && event.order > afterOrder
      && event.order < close.order
    )),
  }
}

/** Install a content-free observer for real browser collaboration frames. */
export function installCollaborationWebSocketObserver(): void {
  type ObserverEvent =
    | { kind: 'close'; code: number; order: number; socketId: number }
    | {
        kind: 'durable_ack'
        observedAt: number
        order: number
        sequence: number
        socketId: number
      }
    | { kind: 'open'; order: number; socketId: number }
    | { kind: 'protocol_error'; order: number; socketId: number }
  type ObserverState = {
    events: ObserverEvent[]
    pendingFrameDecodes: number
  }
  type ObserverWindow = Window & typeof globalThis & {
    __inqtrixCollaborationSocketObserver?: ObserverState
  }

  const observerWindow = window as ObserverWindow
  if (observerWindow.__inqtrixCollaborationSocketObserver) return

  const state: ObserverState = { events: [], pendingFrameDecodes: 0 }
  const NativeWebSocket = window.WebSocket
  let nextOrder = 1
  let nextSocketId = 1

  class Decoder {
    private readonly bytes: Uint8Array
    private offset = 0

    constructor(bytes: Uint8Array) {
      this.bytes = bytes
    }

    get remaining(): number {
      return this.bytes.length - this.offset
    }

    readVarUint(): number {
      let multiplier = 1
      let value = 0
      while (this.offset < this.bytes.length) {
        const byte = this.bytes[this.offset++]!
        value += (byte & 0x7f) * multiplier
        if ((byte & 0x80) === 0) return value
        multiplier *= 128
        if (!Number.isSafeInteger(value) || multiplier > 2 ** 49) break
      }
      throw new Error('invalid varuint')
    }

    readBytes(): Uint8Array {
      const length = this.readVarUint()
      const end = this.offset + length
      if (end > this.bytes.length) throw new Error('truncated bytes')
      const value = this.bytes.subarray(this.offset, end)
      this.offset = end
      return value
    }

    readString(): string {
      return new TextDecoder().decode(this.readBytes())
    }
  }

  const observeFrame = (data: ArrayBuffer, socketId: number, order: number): void => {
    try {
      const bytes = new Uint8Array(data)
      if (bytes.length === 1) return
      const decoder = new Decoder(bytes)
      decoder.readString()
      if (decoder.readVarUint() !== 5) return
      const payload = JSON.parse(decoder.readString()) as unknown
      if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
        throw new Error('invalid stateless payload')
      }
      const message = payload as {
        hash?: unknown
        sequence?: unknown
        type?: unknown
      }
      if (decoder.remaining !== 0) throw new Error('trailing stateless bytes')
      if (message.type !== 'durable_ack') return
      if (
        typeof message.hash !== 'string'
        || !/^[a-f0-9]{64}$/.test(message.hash)
        || !Number.isSafeInteger(message.sequence)
        || Number(message.sequence) <= 0
      ) throw new Error('invalid durable acknowledgement')
      state.events.push({
        kind: 'durable_ack',
        observedAt: Date.now(),
        order,
        sequence: Number(message.sequence),
        socketId,
      })
    } catch {
      state.events.push({ kind: 'protocol_error', order, socketId })
    }
  }

  class ObservedWebSocket extends NativeWebSocket {
    constructor(url: string | URL, protocols?: string | string[]) {
      if (protocols === undefined) super(url)
      else super(url, protocols)

      let collaborationSocket = false
      try {
        collaborationSocket = new URL(String(url), window.location.href).pathname === '/collaboration'
      } catch {
        return
      }
      if (!collaborationSocket) return

      const socketId = nextSocketId++
      this.addEventListener('open', () => {
        state.events.push({ kind: 'open', order: nextOrder++, socketId })
      })
      this.addEventListener('message', (event) => {
        const order = nextOrder++
        if (event.data instanceof ArrayBuffer) {
          observeFrame(event.data, socketId, order)
          return
        }
        if (!(event.data instanceof Blob)) return
        state.pendingFrameDecodes += 1
        void event.data.arrayBuffer()
          .then((buffer) => observeFrame(buffer, socketId, order))
          .finally(() => {
            state.pendingFrameDecodes -= 1
          })
      })
      this.addEventListener('close', (event) => {
        state.events.push({ kind: 'close', code: event.code, order: nextOrder++, socketId })
      })
    }
  }

  observerWindow.WebSocket = ObservedWebSocket
  observerWindow.__inqtrixCollaborationSocketObserver = state
}

export type CollaborationStatusObserverState = {
  /** Jeder Eintritt in den Wiederverbindungs-Zustand, mit Zeitstempel. */
  reconnectAppearances: Array<{ observedAt: number; source: string }>
}

/** Beobachtet DURCHGEHEND, ob der Wiederverbindungs-Streifen erscheint.
 *
 * Warum nicht einfach am Ende `toHaveCount(0)` auf
 * `[data-editor-status-kind="reconnecting"]`? Weil das nur beweist, dass der
 * Streifen im Abtastmoment fehlte. Er ist kein Ein- und Ausbau, sondern ein
 * ATTRIBUTWECHSEL an einem dauerhaft vorhandenen Knoten
 * (`EditorInspector.tsx`), und er kann zwischen zwei Abtastungen kommen und
 * gehen. Abwesenheit ueber eine Zeitspanne laesst sich nur beobachten, nicht
 * abfragen.
 *
 * Wie die Socket-Sonde laeuft dies als `addInitScript` und wirkt erst ab der
 * NAECHSTEN Navigation. Ein Neuladen der Seite setzt das Protokoll zurueck. */
export function installCollaborationStatusObserver(): void {
  type ObserverWindow = Window & typeof globalThis & {
    __inqtrixCollaborationStatusObserver?: CollaborationStatusObserverState
  }
  const observerWindow = window as ObserverWindow
  if (observerWindow.__inqtrixCollaborationStatusObserver) return

  const state: CollaborationStatusObserverState = { reconnectAppearances: [] }
  const RECONNECTING = 'reconnecting'
  const ATTRIBUTE = 'data-editor-status-kind'
  // Es gibt mehr als einen Statusknoten (Kopfzeile und Inspektor). Jeder wird
  // einzeln nachgehalten, damit ein Wechsel am einen nicht als Wechsel am
  // anderen gezaehlt wird.
  const zuletzt = new WeakMap<Element, string | null>()

  const pruefen = (element: Element, source: string): void => {
    const wert = element.getAttribute(ATTRIBUTE)
    const vorher = zuletzt.get(element) ?? null
    if (wert === vorher) return
    zuletzt.set(element, wert)
    if (wert === RECONNECTING) {
      state.reconnectAppearances.push({ observedAt: Date.now(), source })
    }
  }

  const alleZustaende = (source: string): void => {
    for (const element of document.querySelectorAll(`[${ATTRIBUTE}]`)) {
      pruefen(element, source)
    }
  }

  const beobachter = new MutationObserver((records) => {
    for (const record of records) {
      if (record.type === 'attributes' && record.target instanceof Element) {
        pruefen(record.target, 'attribute')
      }
    }
    // Ein frisch eingehaengter Knoten kann bereits im Wiederverbindungs-
    // Zustand geboren werden; ein reiner Attribut-Zweig saehe ihn nie.
    alleZustaende('subtree')
  })

  const starten = (): void => {
    alleZustaende('initial')
    beobachter.observe(document.documentElement, {
      attributeFilter: [ATTRIBUTE],
      attributes: true,
      childList: true,
      subtree: true,
    })
  }

  if (document.documentElement) starten()
  else document.addEventListener('DOMContentLoaded', starten, { once: true })

  observerWindow.__inqtrixCollaborationStatusObserver = state
}
