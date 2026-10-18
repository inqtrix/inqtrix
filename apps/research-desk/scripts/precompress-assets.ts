/** Build-time precompression of static assets — see plugin docstring.
 *
 * Lives in its own module so the offline suite can drive it through a real
 * `vite build` of a minimal fixture: the regression it guards (siblings
 * frozen before chunk finalization) is only observable on BUILT output,
 * never in source. `precompress-assets.test.ts` is that gate's offline twin;
 * `scripts/verify-precompressed-assets.mjs` stays the in-image belt.
 */
import { brotliCompressSync, constants, gzipSync } from 'node:zlib'
import { readdirSync, readFileSync, statSync, writeFileSync } from 'node:fs'
import path from 'node:path'

import type { Plugin } from 'vite'

// Below this size the encoding headers and a second cache entry cost
// more than the saved bytes.
const COMPRESSIBLE_MIN_BYTES = 1_024
const COMPRESSIBLE = /\.(?:js|mjs|css|svg|json|map|txt|wasm)$/

/** Emit `.br`/`.gz` siblings for static assets at BUILD time.
 *
 * The gateway serves these as-is; it never compresses per request,
 * because squeezing a multi-MB bundle on every cache miss costs more
 * than the smaller payload saves. Already-compressed formats (png,
 * woff2, …) are skipped — recompressing them only adds bytes.
 *
 * MUST run in `writeBundle`, from the bytes ON DISK — never from
 * `generateBundle`'s chunk objects. Vite finalizes chunks in that same
 * phase (e.g. replacing the `__VITE_PRELOAD__` marker of transformed
 * dynamic imports), so a sibling compressed from the in-memory code can
 * freeze a pre-final chunk: the plain .js ships correct while every
 * brotli-accepting browser gets code that throws at the first lazy
 * import. That exact failure shipped once; `ui:verify-dist` now pins
 * byte-identity so it cannot ship twice.
 */
export function precompressAssets(): Plugin {
  return {
    apply: 'build',
    name: 'inqtrix-precompress-assets',
    writeBundle(options) {
      const outDir = options.dir ?? path.dirname(options.file ?? 'dist')
      const walk = (dir: string): string[] => readdirSync(dir, { withFileTypes: true })
        .flatMap((entry) => {
          const full = path.join(dir, entry.name)
          return entry.isDirectory() ? walk(full) : [full]
        })
      for (const file of walk(outDir)) {
        if (!COMPRESSIBLE.test(file)) continue
        if (statSync(file).size < COMPRESSIBLE_MIN_BYTES) continue
        const raw = readFileSync(file)
        writeFileSync(`${file}.gz`, gzipSync(raw, { level: 9 }))
        writeFileSync(`${file}.br`, brotliCompressSync(raw, {
          params: {
            [constants.BROTLI_PARAM_QUALITY]: 11,
            [constants.BROTLI_PARAM_SIZE_HINT]: raw.byteLength,
          },
        }))
      }
    },
  }
}
