export const EDITOR_COLLABORATION_PROTOCOL_VERSION = 1
export const EDITOR_SCHEMA_VERSION = 2
export const EDITOR_YJS_FRAGMENT = 'content'
export const EDITOR_ROOM_PREFIX = 'inqtrix-editor-v1'

export const EDITOR_SCHEMA_DEPENDENCY_VERSIONS = Object.freeze({
  editorSchema: '0.1.0',
  hocuspocusTransformer: '4.3.0',
  prosemirrorSuggestChanges: '0.1.8',
  tiptap: '3.31.3',
  tiptapYjs: '3.0.9',
  yjs: '13.6.31',
})

// Bump the matching token whenever compatibility-relevant behavior changes
// without changing a declarative ProseMirror schema field.
export const EDITOR_SCHEMA_BEHAVIOR_INPUTS = Object.freeze({
  extensionConfiguration: 'editor-extensions-v1',
  linkPolicy: 'http-https-mailto-noopener-v1',
  // v2: `parseEditorMarkdown` wendet die LaTeX-Einfuhr-Regel nicht mehr an.
  // Sie gehoert auf FREMDEN Text; auf die eigene Serialisierung angewandt
  // zerstoerte sie Inhalt (`\[Marke\]` wurde zum Formelblock).
  //
  // Der Wert wandert mit, weil der Abdruck auf JEDEM Kollaborationsdokument
  // steht und beim Laden gegen den aktuellen geprueft wird: er beantwortet
  // "wurde dieses Dokument von einem Dienst mit MEINEM Verhalten aktiviert".
  // Die Umwandlung Markdown -> Y.Doc hat sich geaendert, also ist die Antwort
  // fuer Bestandsdokumente ehrlicherweise nein -- Migration
  // 0081_editor_markdown_read stempelt sie um und nimmt sie damit bewusst an.
  //
  // Ausdruecklich NICHT geleistet: eine Schraege zwischen Browser und Dienst.
  // Der Browser berechnet den Fingerabdruck nie (er ist in `browser.ts` nicht
  // einmal exportiert); ueber die Grenze geht nur `schema_version`. Ein alter
  // Browser-Tab mit neuem Dienst bleibt damit unerkannt.
  //
  // v3: inside a table cell the reader takes the line break the serializer
  // writes there (`<br>`) as a hard break instead of the text `<br>`, so the
  // Markdown -> Y.Doc conversion changed again (re-stamped by migration
  // 0092_editor_library_upgrade).
  markdownProjection: 'gfm-final-original-v3-literal-read-table-cell-break',
  relativePositions: 'yjs-relative-position-base64-v1',
  suggestionTransform: 'semantic-five-kind-structure-metadata-table-topology-block-boundary-record-mirror-v7',
  yjsValidation: 'canonical-v1-structure-attributes-full-consumption-live-state-attribute-map-v9',
})

export const EDITOR_BLOCK_SUGGESTIONS_SUPPORTED = true

// Code blocks admit inline suggestion marks on their text content. No block
// parent advertises node-level suggestion support to the upstream transform.
export const EDITOR_SUGGESTION_BLOCK_PARENTS = Object.freeze([
  'codeBlock',
])

export function editorCollaborationRoom(documentId: string, generation: number): string {
  if (!/^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/.test(documentId)) {
    throw new Error('Invalid editor document id')
  }
  if (!Number.isSafeInteger(generation) || generation < 1) {
    throw new Error('Collaboration generation must be a positive integer')
  }
  return `${EDITOR_ROOM_PREFIX}:${documentId}:g${generation}`
}

export function parseEditorCollaborationRoom(room: string): {
  documentId: string
  generation: number
} | null {
  const match = /^inqtrix-editor-v1:([A-Za-z0-9][A-Za-z0-9_-]{0,127}):g([1-9][0-9]*)$/.exec(room)
  if (!match) return null
  const generation = Number(match[2])
  if (!Number.isSafeInteger(generation)) return null
  return { documentId: match[1]!, generation }
}
