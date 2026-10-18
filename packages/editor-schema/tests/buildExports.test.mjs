import { readFile } from 'node:fs/promises'
import { describe, expect, it } from 'vitest'
import * as browser from '../dist/browser.js'
import * as core from '../dist/core.js'
import * as protocol from '../dist/protocolEntry.js'
import * as server from '../dist/server.js'

describe('built package boundaries', () => {
  it('publishes explicit source-backed workspace entry points', async () => {
    const packageJson = JSON.parse(await readFile(
      new URL('../package.json', import.meta.url),
      'utf8',
    ))

    expect(Object.keys(packageJson.exports).sort()).toEqual([
      '.',
      './browser',
      './core',
      './protocol',
      './server',
    ])
    expect(core.EDITOR_SCHEMA_DEPENDENCY_VERSIONS.editorSchema).toBe(packageJson.version)
  })

  it('fingerprints the exact library versions the package pins', async () => {
    // The fingerprint answers "was this document written with MY rules and
    // libraries": a version named here that is not the one installed makes
    // that answer wrong without any test noticing.
    const packageJson = JSON.parse(await readFile(
      new URL('../package.json', import.meta.url),
      'utf8',
    ))
    const pinned = packageJson.dependencies
    const tiptapPins = new Set(Object.entries(pinned)
      .filter(([name]) => name.startsWith('@tiptap/') && name !== '@tiptap/y-tiptap')
      .map(([, version]) => version))

    expect([...tiptapPins]).toEqual([pinned['@tiptap/core']])
    expect(core.EDITOR_SCHEMA_DEPENDENCY_VERSIONS).toEqual({
      editorSchema: packageJson.version,
      hocuspocusTransformer: pinned['@hocuspocus/transformer'],
      prosemirrorSuggestChanges: pinned['@handlewithcare/prosemirror-suggest-changes'],
      tiptap: pinned['@tiptap/core'],
      tiptapYjs: pinned['@tiptap/y-tiptap'],
      yjs: pinned.yjs,
    })
  })

  it('emits usable runtime entry points without crossing browser/server boundaries', () => {
    expect(core.createEditorSchemaExtensions).toBeTypeOf('function')
    expect(core).not.toHaveProperty('createRelativePositionAdapter')
    expect(core).not.toHaveProperty('editorJsonToYDoc')

    expect(browser.createRelativePositionAdapter).toBeTypeOf('function')
    expect(browser).not.toHaveProperty('editorJsonToYDoc')

    expect(server.editorJsonToYDoc).toBeTypeOf('function')
    expect(server).not.toHaveProperty('createRelativePositionAdapter')

    expect(protocol.editorCollaborationRoom).toBeTypeOf('function')
    expect(protocol.EDITOR_COLLABORATION_PROTOCOL_VERSION).toBe(1)
    expect(protocol).not.toHaveProperty('createEditorSchemaExtensions')
  })
})
