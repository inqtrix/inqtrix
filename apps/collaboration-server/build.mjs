import { build } from 'esbuild'

await build({
  entryPoints: ['src/main.ts'],
  bundle: true,
  platform: 'node',
  format: 'cjs',
  target: ['node22'],
  outfile: 'dist/main.cjs',
  sourcemap: true,
  legalComments: 'external',
})
