import { randomUUID } from 'node:crypto'
import { chmod, mkdir, unlink } from 'node:fs/promises'
import { resolve } from 'node:path'

const PROTOCOL = 'inqtrix-verification-resource-v1'

export class VerificationLifecycleClient {
  #descriptors = new Map()
  #pending = new Map()
  #reportDirectory
  #runId
  #sessionSequence = 0

  constructor({ reportDirectory, runId }) {
    if (!process.send || !process.connected) {
      throw new Error('Product fixtures require the verification orchestrator IPC lifecycle.')
    }
    if (typeof reportDirectory !== 'string' || reportDirectory.length === 0) {
      throw new Error('Verification report directory is required.')
    }
    this.#reportDirectory = reportDirectory
    this.#runId = runId
    process.on('message', (message) => this.#receive(message))
    process.once('disconnect', () => {
      for (const pending of this.#pending.values()) {
        pending.reject(new Error('Verification lifecycle IPC disconnected.'))
      }
      this.#pending.clear()
    })
  }

  async register(resource) {
    const handleId = await this.#request({ resource, type: 'register' })
    this.#descriptors.set(handleId, resource)
    return { id: handleId }
  }

  async complete(handle) {
    if (!handle || !this.#descriptors.has(handle.id)) return
    await this.#request({ handleId: handle.id, type: 'complete' })
    this.#descriptors.delete(handle.id)
  }

  async completeDocumentCascade(documentId) {
    const handles = [...this.#descriptors.entries()]
      .filter(([, resource]) => (
        resource.id === documentId || resource.documentId === documentId
      ))
      .map(([id]) => ({ id }))
    for (const handle of handles) await this.complete(handle)
  }

  async registerSession(context, actorLabel) {
    this.#sessionSequence += 1
    const id = [
      'session',
      this.#runId,
      String(this.#sessionSequence).padStart(2, '0'),
    ].join('-')
    const directory = resolve(this.#reportDirectory, '.cleanup-secrets')
    await mkdir(directory, { recursive: true, mode: 0o700 })
    const storageStatePath = resolve(directory, `${id}.json`)
    const handle = await this.register({
      id,
      kind: 'session',
      storageStatePath,
    })
    await context.storageState({ path: storageStatePath })
    await chmod(storageStatePath, 0o600)
    return { actorLabel, handle, storageStatePath }
  }

  async completeSession(session) {
    if (!session) return
    await this.complete(session.handle)
    await unlink(session.storageStatePath).catch(() => undefined)
  }

  close() {
    if (process.connected) process.disconnect()
  }

  async #request(payload) {
    const requestId = randomUUID()
    const response = new Promise((resolvePromise, rejectPromise) => {
      this.#pending.set(requestId, { reject: rejectPromise, resolve: resolvePromise })
    })
    process.send({
      ...payload,
      protocol: PROTOCOL,
      requestId,
      runId: this.#runId,
    })
    return await response
  }

  #receive(message) {
    if (
      !message
      || message.protocol !== PROTOCOL
      || typeof message.requestId !== 'string'
    ) return
    const pending = this.#pending.get(message.requestId)
    if (!pending) return
    this.#pending.delete(message.requestId)
    if (message.type === 'ack') pending.resolve(message.handleId)
    else pending.reject(new Error('Product resource lifecycle operation failed.'))
  }
}
