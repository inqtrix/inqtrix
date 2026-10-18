type RetryEntry = {
  task: () => Promise<void>
  timer: ReturnType<typeof setTimeout> | null
}

export type SnapshotRetryCallbacks = {
  isEligible: (room: string) => boolean
  onFailure: (room: string, error: unknown) => void
  onSuccess: (room: string) => void
}

export class SnapshotRetryController {
  private readonly entries = new Map<string, RetryEntry>()

  constructor(
    private readonly baseDelayMs: number,
    private readonly maximumDelayMs: number,
    private readonly callbacks: SnapshotRetryCallbacks,
  ) {}

  schedule(room: string, task: () => Promise<void>): void {
    if (!this.callbacks.isEligible(room)) {
      this.cancel(room)
      return
    }
    const existing = this.entries.get(room)
    if (existing) {
      existing.task = task
      return
    }
    this.scheduleAttempt(room, task, 0)
  }

  cancel(room: string): void {
    const existing = this.entries.get(room)
    if (existing?.timer) clearTimeout(existing.timer)
    this.entries.delete(room)
  }

  cancelAll(): void {
    for (const entry of this.entries.values()) {
      if (entry.timer) clearTimeout(entry.timer)
    }
    this.entries.clear()
  }

  isPending(room: string): boolean {
    return this.entries.has(room)
  }

  private scheduleAttempt(
    room: string,
    task: () => Promise<void>,
    attempt: number,
  ): void {
    const delay = Math.min(
      this.maximumDelayMs,
      this.baseDelayMs * (2 ** Math.min(attempt, 30)),
    )
    const timer = setTimeout(() => {
      const current = this.entries.get(room)
      if (!current || current.timer !== timer) return
      current.timer = null
      if (!this.callbacks.isEligible(room)) {
        this.entries.delete(room)
        return
      }
      void current.task().then(
        () => {
          this.cancel(room)
          this.callbacks.onSuccess(room)
        },
        (error: unknown) => {
          if (this.entries.get(room) !== current) return
          this.callbacks.onFailure(room, error)
          if (this.callbacks.isEligible(room)) {
            this.scheduleAttempt(room, current.task, attempt + 1)
          } else {
            this.entries.delete(room)
          }
        },
      )
    }, delay)
    timer.unref()
    this.entries.set(room, { task, timer })
  }
}
