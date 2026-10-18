import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { brotliDecompressSync, gunzipSync } from 'node:zlib'

import { build } from 'vite'
import { afterAll, describe, expect, it } from 'vitest'

import { precompressAssets } from './precompress-assets'

// Offline twin of the in-image `verify-precompressed-assets.mjs` gate.
//
// The regression this pins shipped once: the plugin compressed during
// `generateBundle`, the same phase in which Vite finalizes chunks by
// replacing the `__VITE_PRELOAD__` markers of transformed dynamic imports.
// The plain .js shipped correct while the .br/.gz siblings froze the
// pre-final code — every brotli-accepting browser crashed at the first lazy
// import, invisible to any source-level test. Only a REAL `vite build` of a
// fixture with a dynamic import can observe that class, so that is exactly
// what this test runs.

const fixtureDir = mkdtempSync(path.join(tmpdir(), 'inqtrix-precompress-'))
const outDir = path.join(fixtureDir, 'dist')

afterAll(() => {
  rmSync(fixtureDir, { force: true, recursive: true })
})

async function buildFixture(): Promise<void> {
  // Padding pushes both chunks past the plugin's 1 KiB floor so siblings
  // exist for exactly the files whose finalization matters.
  const padding = `export const padding = ${JSON.stringify('x'.repeat(2_000))}\n`
  writeFileSync(path.join(fixtureDir, 'entry.js'),
    `${padding}export async function load() { return import('./lazy.js') }\nload()\n`)
  writeFileSync(path.join(fixtureDir, 'lazy.js'),
    `${padding}export const answer = 42\n`)
  await build({
    build: {
      outDir,
      rollupOptions: { input: path.join(fixtureDir, 'entry.js') },
    },
    configFile: false,
    logLevel: 'silent',
    plugins: [precompressAssets()],
    root: fixtureDir,
  })
}

describe('precompressAssets on a real vite build', () => {
  it('every sibling is byte-identical to its finalized source file', async () => {
    await buildFixture()

    const assets = path.join(outDir, 'assets')
    const { readdirSync } = await import('node:fs')
    const siblings = readdirSync(assets).filter((f) => f.endsWith('.br') || f.endsWith('.gz'))
    expect(siblings.length).toBeGreaterThan(0)

    for (const sibling of siblings) {
      const compressed = readFileSync(path.join(assets, sibling))
      const plain = readFileSync(path.join(assets, sibling.slice(0, -3)))
      const decompressed = sibling.endsWith('.br')
        ? brotliDecompressSync(compressed)
        : gunzipSync(compressed)
      expect(decompressed.equals(plain), `${sibling} weicht von seiner Quelle ab`).toBe(true)
      // The marker Vite replaces at finalization must never survive into a
      // served variant — this is the exact byte pattern that shipped.
      expect(decompressed.includes('__VITE_PRELOAD__'), `${sibling} trägt __VITE_PRELOAD__`).toBe(false)
    }
  }, 30_000)
})
