import { createHash } from 'node:crypto'
import { assertFixture as assert, fetchActorJson as fetchJson } from './api.mjs'

/** Fictional operating material, with distinct decisions at both ends. */
export function documentConversationMaterial() {
  const responsibilities = [
    ['Support', 'Serviceleitung', 'Kundenanfragen'],
    ['Vertrieb', 'Vertriebsleitung', 'Angebotsentwürfe'],
    ['Einkauf', 'Einkaufsleitung', 'Lieferantenvergleiche'],
    ['Finanzen', 'Controlling', 'Kostenübersichten'],
    ['Technik', 'Technische Leitung', 'Wartungsnotizen'],
    ['Wissensmanagement', 'Redaktionsleitung', 'interne Leitfäden'],
    ['Schulung', 'Weiterbildungskoordination', 'Übungsunterlagen'],
    ['Datenschutz', 'Datenschutzbeauftragte', 'Löschanfragen'],
    ['Informationssicherheit', 'Sicherheitsverantwortliche', 'Zugriffsprüfungen'],
    ['Qualitätsmanagement', 'Qualitätsleitung', 'Abweichungsprotokolle'],
    ['Betrieb', 'Betriebsleitung', 'Übergabeprotokolle'],
    ['Projektsteuerung', 'Projektleitung', 'Entscheidungsvorlagen'],
    ['Redaktion', 'Fachredaktion', 'Veröffentlichungsentwürfe'],
    ['Logistik', 'Logistikleitung', 'Transportnotizen'],
    ['Servicepartner', 'Partnermanagement', 'Leistungsbeschreibungen'],
    ['Forschung', 'Forschungskoordination', 'Literaturübersichten'],
    ['Produktmanagement', 'Produktleitung', 'Anforderungskataloge'],
    ['Dokumentation', 'Dokumentationsleitung', 'Arbeitsanweisungen'],
    ['Portfoliosteuerung', 'Portfoliomanagement', 'Projektübersichten'],
    ['Standortbetrieb', 'Standortleitung', 'Betriebsmitteilungen'],
    ['Kundenschulung', 'Schulungsleitung', 'Teilnahmeinformationen'],
    ['Interne Revision', 'Revisionsleitung', 'Prüffeststellungen'],
    ['Kommunikation', 'Kommunikationsleitung', 'Informationsblätter'],
    ['Archivierung', 'Archivverantwortliche', 'Aufbewahrungsübersichten'],
  ]
  const sections = responsibilities.map(([area, role, material], index) => `## ${index + 1}. ${area}

Für ${material} verantwortet die ${role} die fachliche Freigabe. Der Assistent darf Vorschläge erarbeiten, vorhandene Unterlagen durchsuchen und Unterschiede nachvollziehbar darstellen. Der Vorschlag wird zunächst im internen Arbeitsbereich abgelegt. Eine Formulierung des Assistenten ersetzt weder die Prüfung der Originalunterlagen noch eine Entscheidung der zuständigen Person. Die Bearbeitung muss erkennen lassen, welche Aussagen aus bereitgestellten Quellen stammen und welche lediglich eine empfohlene Vorgehensweise beschreiben.

Vor einer Bearbeitung prüft ${area} den Auftrag, den Empfängerkreis und den Stand der Unterlagen. Bei mehreren Fassungen ist die freigegebene Fassung ausdrücklich zu benennen. Ein Suchtreffer ist ein Einstieg in die Prüfung und kein Vollständigkeitsnachweis. Widersprechen sich zwei Unterlagen, wird die Abweichung mit den betroffenen Stellen festgehalten. Es darf nicht stillschweigend die bequemere Aussage übernommen werden. Fehlende Angaben werden als offene Frage erfasst und der verantwortlichen Person zugeordnet.

Die ${role} dokumentiert Anlass, betroffene Unterlagen, Ergebnis und verbleibende Einschränkungen. Ein erfolgreicher technischer Aufruf ist noch keine fachliche Freigabe. Eine stichprobenartige Kontrolle muss zu den Originalstellen zurückführen. Kann ein Dokument nicht gelesen werden, bleibt dieser Teil des Auftrags offen. Die übrige Arbeit darf fortgeführt werden, wenn ihre eigenen Voraussetzungen erfüllt sind. Die Abschlussnotiz unterscheidet deshalb erledigte Punkte, ungeprüfte Annahmen und Entscheidungen, die weiterhin bei Menschen liegen.

Bei einer Übergabe erhält die nächste zuständige Person den letzten bestätigten Stand und die zugehörigen Belege. Bereits genehmigte Inhalte werden nicht allein wegen einer Umformulierung erneut als neue Erkenntnis dargestellt. Ein zurückgezogener Entwurf bleibt als zurückgezogen gekennzeichnet. Die organisatorische Ablage von ${material} folgt dem bestehenden Berechtigungskonzept. Zusätzliche Kopien in persönlichen Ordnern sind kein Ersatz für den gemeinsamen Arbeitsstand. Abweichende Freigaben am Ende dieses Leitfadens haben Vorrang vor dem allgemeinen Ablauf.

Die Prüfung im Bereich ${area} umfasst Lesbarkeit, korrekte Zuordnung, erforderliche Anhänge und eine verständliche Statusmeldung. Die ${role} benennt eine Vertretung, bevor der Vorgang übergeben wird. Bei Abbruch muss nachvollziehbar bleiben, welche Arbeit bereits gespeichert wurde. Nach einer Wiederaufnahme wird an diesem bestätigten Stand weitergearbeitet. Die fachliche Verantwortung und die technische Ausführung werden in der Entscheidungsvorlage getrennt angegeben.
`)
  const header = '# Interner Einführungsleitfaden\n\n'
    + 'Fiktives Arbeitsmaterial für die Einführung eines Assistenzsystems. '
    + 'Die IT-Leitung koordiniert die Einführung; die Datenschutzbeauftragte wird beteiligt. '
    + 'Es werden keine Personalentscheidungen automatisiert. Der folgende allgemeine Ablauf gilt vorbehaltlich der abschließenden Sonderfreigabe.\n\n'
  const common = header + sections.join('\n')
  const knowledge = common + '\n## Abschließende Sonderfreigabe\n\n'
    + 'Für den Pilotbetrieb gilt ein gesonderter Sachkostenrahmen von 7.800 EUR. '
    + 'Die Freigabe erfolgt durch genau zwei fachlich unterschiedliche Personen. '
    + 'Der Pilot darf ausschließlich dienstags und donnerstags zwischen 09:00 und 15:00 Uhr betrieben werden. '
    + 'Ein Einsatz am Freitag ist nicht freigegeben. Vor einer Erweiterung ist ein schriftlicher Beschluss der IT-Leitung erforderlich.\n'
  const editor = common + '\n## Abschließende Sonderfreigabe\n\n'
    + 'Für den Pilotbetrieb gilt ein gesonderter Sachkostenrahmen von 6.900 EUR. '
    + 'Die Freigabe erfolgt durch genau eine Person. '
    + 'Der Pilot darf dienstags, donnerstags und freitags zwischen 09:00 und 15:00 Uhr betrieben werden. '
    + 'Eine Erweiterung kann mündlich mit der IT-Leitung vereinbart werden.\n'
  assert([...knowledge].length > 40_000 && [...knowledge].length < 100_000, 'Document material must span several ordinary read windows.')
  // Source intake normalizes boundary whitespace. Supply canonical fixture
  // text so the exact persisted-text assertion still detects any other loss.
  return { knowledge: knowledge.trim(), editor: editor.trim() }
}

/**
 * Fictional acceptance record; its second page is a scan without a text layer.
 * The pages do not number themselves: only the extraction facts can tell a
 * reader that a page between them is missing from the text.
 */
export const ATTACHMENT_PAGES = [
  'Anlage: Prüfprotokoll zum Pilotbetrieb\nErgebnis der Abnahme\n'
    + 'Die Abnahme des Pilotbetriebs fand im Schulungsraum 2 statt.\n'
    + 'Geprüft wurden 14 Arbeitsplätze; 13 bestanden ohne Beanstandung.\n'
    + 'Am Arbeitsplatz 9 fehlte die Freigabe der Fachredaktion.',
  null,
  'Nacharbeiten\n'
    + 'Die fehlende Freigabe wird innerhalb von zehn Arbeitstagen nachgeholt.\n'
    + 'Verantwortlich ist die Dokumentationsleitung.\n'
    + 'Die Wiederholungsprüfung umfasst nur den Arbeitsplatz 9.',
]

/**
 * A minimal PDF 1.4 with one page per entry. A `null` page only paints a grey
 * box: like a scanned page it is part of the file, but a text extractor finds
 * nothing on it. Text is WinAnsi (Latin-1 covers the German characters used).
 */
export function buildPdf(pages) {
  const objects = [
    Buffer.from('<< /Type /Catalog /Pages 2 0 R >>'),
    null,
    Buffer.from('<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>'),
  ]
  const kids = []
  for (const text of pages) {
    const stream = text === null
      ? Buffer.from('0.85 g 72 400 451 300 re f')
      : Buffer.concat([
        Buffer.from('BT /F1 12 Tf 14 TL 72 720 Td\n'),
        ...text.split('\n').map((line) => Buffer.concat([
          Buffer.from('('),
          Buffer.from(line.replace(/[\\()]/g, (character) => `\\${character}`), 'latin1'),
          Buffer.from(') Tj T*\n'),
        ])),
        Buffer.from('ET'),
      ])
    const pageNumber = objects.length + 1
    kids.push(pageNumber)
    objects.push(Buffer.from('<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] '
      + `/Resources << /Font << /F1 3 0 R >> >> /Contents ${pageNumber + 1} 0 R >>`))
    objects.push(Buffer.concat([
      Buffer.from(`<< /Length ${stream.length} >>\nstream\n`), stream, Buffer.from('\nendstream'),
    ]))
  }
  objects[1] = Buffer.from(`<< /Type /Pages /Kids [${kids.map((number) => `${number} 0 R`).join(' ')}] /Count ${kids.length} >>`)
  const parts = [Buffer.from('%PDF-1.4\n%\u00e2\u00e3\u00cf\u00d3\n', 'latin1')]
  const offsets = []
  let length = parts[0].length
  objects.forEach((body, index) => {
    offsets.push(length)
    const object = Buffer.concat([Buffer.from(`${index + 1} 0 obj\n`), body, Buffer.from('\nendobj\n')])
    parts.push(object)
    length += object.length
  })
  parts.push(Buffer.from(`xref\n0 ${objects.length + 1}\n0000000000 65535 f \n`
    + offsets.map((offset) => `${String(offset).padStart(10, '0')} 00000 n \n`).join('')
    + `trailer\n<< /Size ${objects.length + 1} /Root 1 0 R >>\nstartxref\n${length}\n%%EOF\n`))
  return Buffer.concat(parts)
}

/**
 * Upload the attachment through the product's own library path: default
 * sections, reservation, bound multipart, durable upload operation. The
 * worker's canonical parse is the only producer of the text that knowledge
 * ingestion then reserves by `asset_id`.
 */
async function createFileAttachment({ actor, lifecycle, runId, retainForReview, waitFor, collectionId }) {
  const title = `${runId} Anlage Prüfprotokoll Pilotbetrieb`
  const fileName = 'pruefprotokoll-pilotbetrieb.pdf'
  const pdf = buildPdf(ATTACHMENT_PAGES)
  const sections = await fetchJson(actor, 'PUT', '/v1/assets/default-sections')
  const library = sections.data.find((section) => section.semantic_role === 'library')
  assert(library, 'The file library has no default library section.')
  const assetId = `asset-${runId}-pruefprotokoll`
  const now = Date.now() / 1000
  const binding = { section_id: library.id, title, label: 'pruefprotokoll', origin: 'library' }
  // The asset id is chosen here, so the cleanup owner exists before the asset does.
  await lifecycle.register({ kind: 'asset', id: assetId, title, credential: 'user', ownerEmail: actor.email, retainForReview })
  await fetchJson(actor, 'POST', `/v1/assets/${assetId}/upload-reservation`, {
    data: { ...binding, group_id: null, file_name: fileName, mime_type: 'application/pdf', page_count: null,
      parse_status: 'parsed', parse_warning: null, text_truncated: false, size_bytes: pdf.length,
      parser_id: null, created_at: now, updated_at: now },
    expected: [201],
  })
  const uploaded = await fetchJson(actor, 'POST', '/v1/files', {
    multipart: { file: { name: fileName, mimeType: 'application/pdf', buffer: pdf }, asset_id: assetId,
      ...binding, created_at: String(now), updated_at: String(now) },
    expected: [200, 201, 202],
  })
  const operationId = uploaded.upload_operation?.operation_id
  assert(operationId, 'The bound upload returned no durable upload operation.')
  const operation = await waitFor(async () => {
    const current = await fetchJson(actor, 'GET', `/v1/uploads/${operationId}`)
    return ['ready', 'upload_failed'].includes(current.status) ? current : null
  }, 300_000, 'canonical file preparation')
  assert(operation.status === 'ready', `File preparation ended ${operation.status}: ${JSON.stringify(operation.error)}`)
  const asset = await fetchJson(actor, 'GET', `/v1/assets/${assetId}`)
  assert(asset.prepared_parser_id && asset.prepared_parser_revision,
    'The prepared text does not name the parser revision that produced it.')
  assert(asset.prepared_content_hash === createHash('sha256').update(asset.prepared_text).digest('hex'),
    'The prepared text does not match its recorded digest.')

  const job = await fetchJson(actor, 'POST', `/v1/knowledge/collections/${collectionId}/document-revisions`, {
    data: { asset_id: assetId, title, workspace_id: actor.workspaceId,
      metadata: { source: 'verification', run_id: runId } }, expected: [202],
  })
  const terminal = await waitFor(async () => {
    const current = await fetchJson(actor, 'GET', `/v1/knowledge/indexing-jobs/${job.job_id}`)
    return ['completed', 'failed', 'cancelled'].includes(current.status) ? current : null
  }, 300_000, 'attachment publication')
  assert(terminal.status === 'completed', `Attachment publication ended ${terminal.status}.`)
  const documentId = terminal.document_id ?? job.document_id
  const stored = await fetchJson(actor, 'GET', `/v1/knowledge/documents/${documentId}/text`)
  const extraction = stored.metadata?.extraction
  // Ingestion reserves the preparation only after verifying it against the
  // file record, so the file digest in the facts is the uploaded file's.
  assert(extraction?.file_sha256 === createHash('sha256').update(pdf).digest('hex')
    && extraction.parser_revision === asset.prepared_parser_revision && extraction.pages === ATTACHMENT_PAGES.length
    && JSON.stringify(extraction.pages_without_text) === '[2]',
    `The knowledge document does not carry the extraction facts of its file: ${JSON.stringify(extraction)}`)
  assert(stored.text === asset.prepared_text, 'The knowledge document differs from the canonical prepared text.')
  return { asset_id: assetId, file_id: asset.server_file_id, document_id: documentId, title, extraction,
    prepared_text: asset.prepared_text }
}

export async function createAgentDocumentMaterial({ actor, lifecycle, runId, retainForReview, waitFor, schemaVersion }) {
  assert(Number.isInteger(schemaVersion), 'Document acceptance requires the published collaboration schema.')
  const text = documentConversationMaterial()
  const name = `${runId} Einführungsunterlagen`
  const collection = await fetchJson(actor, 'POST', '/v1/knowledge/collections', {
    data: { name }, expected: [201],
  })
  await lifecycle.register({ kind: 'knowledge_collection', id: collection.id, name,
    credential: 'user', ownerEmail: actor.email, retainForReview })
  const job = await fetchJson(actor, 'POST', `/v1/knowledge/collections/${collection.id}/document-revisions`, {
    data: { title: `${runId} Freigegebener Einführungsleitfaden`, text: text.knowledge,
      workspace_id: actor.workspaceId, metadata: { source: 'verification', run_id: runId } }, expected: [202],
  })
  const terminal = await waitFor(async () => {
    const current = await fetchJson(actor, 'GET', `/v1/knowledge/indexing-jobs/${job.job_id}`)
    return ['completed', 'failed', 'cancelled'].includes(current.status) ? current : null
  }, 300_000, 'knowledge document publication')
  assert(terminal.status === 'completed', `Knowledge publication ended ${terminal.status}.`)
  const documentId = terminal.document_id ?? job.document_id
  assert(documentId, 'Knowledge publication returned no document ID.')
  const stored = await fetchJson(actor, 'GET', `/v1/knowledge/documents/${documentId}/text`)
  assert(stored.text === text.knowledge, 'Published knowledge text differs from the submitted original.')

  const editorId = `ed_${runId}_working_guide`
  const title = `${runId} Arbeitsfassung Einführungsleitfaden`
  await lifecycle.register({ kind: 'document', id: editorId, credential: 'user', ownerEmail: actor.email, retainForReview })
  const now = Date.now() / 1000
  const editor = await fetchJson(actor, 'PUT', `/v1/editor/documents/${editorId}`, {
    data: { title, content_markdown: text.editor, source: 'blank', revision: 1, created_at: now, updated_at: now },
  })
  assert(editor.content_markdown === text.editor, 'Stored editor text differs from the submitted original.')
  await fetchJson(actor, 'POST', `/v1/editor/documents/${editorId}/collaboration:enable`, {
    data: { expected_revision: editor.revision, expected_metadata_revision: editor.metadata_revision,
      schema_version: schemaVersion },
  })
  const current = await fetchJson(actor, 'GET', `/v1/editor/documents/${editorId}`)
  assert(current.content_mode === 'collaboration', 'Editor target did not enter collaboration mode.')
  const attachment = await createFileAttachment({ actor, lifecycle, runId, retainForReview, waitFor, collectionId: collection.id })
  const originals = { knowledge: text.knowledge, editor: current.content_markdown }
  return { collection_id: collection.id, collection_name: name, document_id: documentId,
    editor_id: editorId, editor_title: title, originals, editor_input: text.editor, attachment,
    hashes: Object.fromEntries(Object.entries(originals).map(([kind, body]) => [kind, createHash('sha256').update(body).digest('hex')])) }
}
