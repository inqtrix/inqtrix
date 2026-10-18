/// <reference types="vite/client" />

// Build-time constant from the `define` block in vite.config.ts,
// single-sourced from `__display_version__` in src/inqtrix/__init__.py.
declare const __APP_VERSION__: string

// mammoth ships lib/index.d.ts next to its main entry, and its package.json
// "browser" field swaps two internal modules for their browser builds. This
// ambient declaration takes precedence over the package's types and narrows
// them to the one call we use.
declare module 'mammoth' {
  export function extractRawText(input: { arrayBuffer: ArrayBuffer }): Promise<{
    messages: unknown[]
    value: string
  }>
}
