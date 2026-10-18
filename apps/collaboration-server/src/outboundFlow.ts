export type OutboundReservation = {
  bytes: number
  released: boolean
  transportId: string
}

export type OutboundSocket<Payload> = {
  bufferedAmount: number
  readyState: number
  send(payload: Payload, callback: (error?: Error) => void): void
}

export class OutboundFlowController<Payload> {
  private readonly usage = new Map<string, { bytes: number; frames: number }>()

  constructor(
    private readonly maximumFrames: number,
    private readonly maximumBytes: number,
    private readonly maximumBufferedBytes: number,
    private readonly openReadyState: number,
    private readonly onUsageChange: (bytes: number, frames: number) => void,
  ) {}

  reserve(
    transportId: string,
    socket: OutboundSocket<Payload>,
    bytes: number,
  ): OutboundReservation | null {
    const current = this.usage.get(transportId) ?? { bytes: 0, frames: 0 }
    if (
      socket.readyState !== this.openReadyState
      || socket.bufferedAmount + bytes > this.maximumBufferedBytes
      || current.bytes + bytes > this.maximumBytes
      || current.frames + 1 > this.maximumFrames
    ) return null

    const reservation: OutboundReservation = {
      bytes,
      released: false,
      transportId,
    }
    this.usage.set(transportId, {
      bytes: current.bytes + bytes,
      frames: current.frames + 1,
    })
    this.publishUsage()
    return reservation
  }

  send(
    reservation: OutboundReservation,
    socket: OutboundSocket<Payload>,
    payload: Payload,
    onFailure: (error?: Error) => void,
  ): boolean {
    if (
      reservation.released
      || socket.readyState !== this.openReadyState
      || socket.bufferedAmount + reservation.bytes > this.maximumBufferedBytes
    ) {
      this.release(reservation)
      return false
    }
    try {
      socket.send(payload, (error) => {
        this.release(reservation)
        if (error) onFailure(error)
      })
      return true
    } catch (error) {
      this.release(reservation)
      onFailure(error instanceof Error ? error : undefined)
      return false
    }
  }

  release(reservation: OutboundReservation): void {
    if (reservation.released) return
    reservation.released = true
    const current = this.usage.get(reservation.transportId)
    if (!current) return
    const next = {
      bytes: Math.max(0, current.bytes - reservation.bytes),
      frames: Math.max(0, current.frames - 1),
    }
    if (next.bytes === 0 && next.frames === 0) {
      this.usage.delete(reservation.transportId)
    } else {
      this.usage.set(reservation.transportId, next)
    }
    this.publishUsage()
  }

  clear(transportId: string): void {
    if (!this.usage.delete(transportId)) return
    this.publishUsage()
  }

  private publishUsage(): void {
    let bytes = 0
    let frames = 0
    for (const usage of this.usage.values()) {
      bytes += usage.bytes
      frames += usage.frames
    }
    this.onUsageChange(bytes, frames)
  }
}
