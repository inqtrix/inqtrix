#!/usr/bin/env node
/**
 * Gate: every precompressed sibling in dist/ must decompress to EXACTLY the
 * bytes of the file it stands in for.
 *
 * Why this exists: the precompress plugin once ran during `generateBundle`,
 * before Vite finalized chunks (the `__VITE_PRELOAD__` marker of transformed
 * dynamic imports is replaced in that same phase). The plain .js shipped
 * correct while the .br/.gz siblings froze the pre-final code — so every
 * brotli-accepting browser crashed at the first lazy import (mermaid, the
 * PDF viewer, …) and no plain-file inspection could see it. Byte-identity is
 * the only honest check for this class.
 *
 * Runs in the web image build right after `vite build`; a mismatch fails the
 * image, never the runtime.
 */

import { readdirSync, readFileSync } from 'node:fs'
import path from 'node:path'
import process from 'node:process'
import { brotliDecompressSync, gunzipSync } from 'node:zlib'

const distDir = process.argv[2] ?? 'dist'

function walk(dir) {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const full = path.join(dir, entry.name)
    return entry.isDirectory() ? walk(full) : [full]
  })
}

const files = walk(distDir)
let checked = 0
const failures = []

for (const file of files) {
  const isBr = file.endsWith('.br')
  const isGz = file.endsWith('.gz')
  if (!isBr && !isGz) continue
  const plain = file.slice(0, -3)
  let original
  try {
    original = readFileSync(plain)
  } catch {
    failures.push(`${file}: plain sibling ${plain} is missing`)
    continue
  }
  const decompressed = isBr
    ? brotliDecompressSync(readFileSync(file))
    : gunzipSync(readFileSync(file))
  if (!decompressed.equals(original)) {
    failures.push(
      `${file}: decompressed bytes differ from ${path.basename(plain)} `
      + `(${decompressed.byteLength} vs ${original.byteLength} bytes)`,
    )
  }
  checked += 1
}

if (checked === 0) {
  console.error(`verify-precompressed-assets: no .br/.gz siblings found under ${distDir} — `
    + 'the precompress plugin did not run; refusing to pass an empty check.')
  process.exit(1)
}

if (failures.length > 0) {
  console.error(`verify-precompressed-assets: ${failures.length} stale sibling(s):`)
  for (const failure of failures) console.error(`  - ${failure}`)
  process.exit(1)
}

console.log(`verify-precompressed-assets: ${checked} sibling(s) byte-identical under ${distDir}.`)
