import { chmod, mkdir, rename, unlink, writeFile } from 'node:fs/promises'
import { dirname } from 'node:path'

export async function writePrivateJsonFixture(path, value) {
  const directory = dirname(path)
  const temporaryPath = `${path}.tmp`
  await mkdir(directory, { recursive: true, mode: 0o700 })
  try {
    await writeFile(
      temporaryPath,
      `${JSON.stringify(value)}\n`,
      { encoding: 'utf8', mode: 0o600 },
    )
    await rename(temporaryPath, path)
    await chmod(path, 0o600)
  } catch (error) {
    await unlink(temporaryPath).catch(() => undefined)
    throw error
  }
}
