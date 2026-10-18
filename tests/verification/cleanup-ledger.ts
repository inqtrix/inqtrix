import { chmod, rename, writeFile } from 'node:fs/promises'
import { resolve } from 'node:path'

import type {
  CleanupRecord,
  CleanupDisposition,
  CleanupStatus,
} from './model.ts'
import type { Redactor } from './redaction.ts'

type CleanupAction = () => Promise<CleanupDisposition>

type InternalRecord = CleanupRecord & {
  cleanup: CleanupAction
}

export type CleanupHandle = {
  id: string
}

export class CleanupLedger {
  private readonly filePath: string
  private readonly records: InternalRecord[] = []
  private readonly redactor: Redactor
  private sequence = 0

  constructor(reportDirectory: string, redactor: Redactor) {
    this.filePath = resolve(reportDirectory, 'cleanup-ledger.json')
    this.redactor = redactor
  }

  async register(
    kind: CleanupRecord['kind'],
    label: string,
    cleanup: CleanupAction,
  ): Promise<CleanupHandle> {
    this.sequence += 1
    const id = `cleanup-${String(this.sequence).padStart(3, '0')}`
    this.records.push({
      cleanup,
      completedAt: null,
      id,
      kind,
      label: this.redactor.redactMessage(label),
      registeredAt: new Date().toISOString(),
      status: 'registered',
    })
    await this.flush()
    return { id }
  }

  async complete(handle: CleanupHandle): Promise<void> {
    const record = this.required(handle)
    record.completedAt = new Date().toISOString()
    record.status = 'cleaned'
    await this.flush()
  }

  async cleanupAll(): Promise<CleanupRecord[]> {
    for (const record of [...this.records].reverse()) {
      if (record.status === 'cleaned' || record.status === 'retained') continue
      await this.setStatus(record, 'running')
      try {
        const disposition = await record.cleanup()
        if (disposition === 'retained' && record.kind !== 'resource') {
          throw new Error('Only product resources may be retained for review.')
        }
        record.status = disposition === 'retained' ? 'retained' : 'cleaned'
      } catch {
        record.status = 'failed'
      }
      record.completedAt = new Date().toISOString()
      await this.flush()
    }
    await this.flush()
    return this.snapshot()
  }

  snapshot(): CleanupRecord[] {
    return this.records.map(({ cleanup: _cleanup, ...record }) => ({ ...record }))
  }

  private required(handle: CleanupHandle): InternalRecord {
    const record = this.records.find((candidate) => candidate.id === handle.id)
    if (!record) throw new Error(`Unknown cleanup handle: ${handle.id}`)
    return record
  }

  private async setStatus(record: InternalRecord, status: CleanupStatus): Promise<void> {
    record.status = status
    await this.flush()
  }

  private async flush(): Promise<void> {
    const temporaryPath = `${this.filePath}.tmp`
    const payload = `${JSON.stringify(this.redactor.redact(this.snapshot()), null, 2)}\n`
    await writeFile(temporaryPath, payload, { encoding: 'utf8', mode: 0o600 })
    await rename(temporaryPath, this.filePath)
    await chmod(this.filePath, 0o600)
  }
}
