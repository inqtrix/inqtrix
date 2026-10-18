import { readFileSync } from 'node:fs'
import path from 'node:path'

import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

import { precompressAssets } from './scripts/precompress-assets'

const apiProxyTarget = process.env.VITE_INQTRIX_API_BASE_URL || 'http://localhost:5100'

// `__APP_VERSION__` mirrors `__display_version__` of the Python package (the
// release line plus its dev designation, which is what the app shows);
// deploy/docker/Dockerfile.web copies the file into the SPA build.
function readAppVersion(): string {
  const source = path.resolve(__dirname, '../../src/inqtrix/__init__.py')
  let content: string
  try {
    content = readFileSync(source, 'utf8')
  } catch (cause) {
    throw new Error(
      `__APP_VERSION__: cannot read ${source}; builds outside the repo checkout must provide the version source (see the ui-build COPY in deploy/docker/Dockerfile.web)`,
      { cause },
    )
  }
  const match = content.match(/^__display_version__\s*=\s*['"]([^'"]+)['"]/m)
  if (!match) {
    throw new Error(
      `__APP_VERSION__: no __display_version__ assignment found in ${source}`,
    )
  }
  return match[1]
}

// https://vite.dev/config/
export default defineConfig({
  define: {
    __APP_VERSION__: JSON.stringify(readAppVersion()),
  },
  plugins: [react(), tailwindcss(), precompressAssets()],
  resolve: {
    alias: [
      {
        find: '@',
        replacement: path.resolve(__dirname, './src'),
      },
    ],
  },
  server: {
    port: 5173,
    proxy: {
      '/collaboration': {
        target: apiProxyTarget,
        ws: true,
      },
      '/health': apiProxyTarget,
      '/v1': apiProxyTarget,
      '/api': apiProxyTarget,
    },
  },
})
