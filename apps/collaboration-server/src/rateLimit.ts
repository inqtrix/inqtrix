export class SlidingWindowRateLimiter {
  private readonly events = new Map<string, number[]>()

  constructor(
    private readonly limit: number,
    private readonly windowMs: number,
  ) {}

  consume(key: string, now = Date.now()): boolean {
    const threshold = now - this.windowMs
    const current = (this.events.get(key) ?? []).filter((value) => value > threshold)
    if (current.length >= this.limit) {
      this.events.set(key, current)
      return false
    }
    current.push(now)
    this.events.set(key, current)
    return true
  }

  delete(key: string): void {
    this.events.delete(key)
  }
}

export class SessionRegistry {
  private readonly sessions = new Map<string, Set<string>>()

  constructor(private readonly maximum: number) {}

  add(documentId: string, userId: string, socketId: string): boolean {
    const key = `${documentId}:${userId}`
    const sessions = this.sessions.get(key) ?? new Set<string>()
    if (!sessions.has(socketId) && sessions.size >= this.maximum) return false
    sessions.add(socketId)
    this.sessions.set(key, sessions)
    return true
  }

  delete(documentId: string, userId: string, socketId: string): void {
    const key = `${documentId}:${userId}`
    const sessions = this.sessions.get(key)
    if (!sessions) return
    sessions.delete(socketId)
    if (sessions.size === 0) this.sessions.delete(key)
  }
}
