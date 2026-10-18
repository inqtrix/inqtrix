import path from 'node:path'

import { defineConfig } from 'vitest/config'

// Pure-logic unit tests (node environment, no DOM). Mirrors the `@` alias from
// vite.config.ts so specs can import application modules the same way the app
// does. Component rendering is verified in the browser (preview), not here.
export default defineConfig({
  resolve: {
    alias: [
      {
        find: '@',
        replacement: path.resolve(__dirname, './src'),
      },
    ],
  },
  test: {
    environment: 'node',
    // scripts/ carries the vite-plugin contracts (they drive a real `vite
    // build` of a fixture — their regressions are only observable on built
    // output, never in src).
    include: ['src/**/*.test.ts', 'src/**/*.test.tsx', 'scripts/**/*.test.ts'],
  },
})
