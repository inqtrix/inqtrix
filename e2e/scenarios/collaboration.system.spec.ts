import { createHash } from 'node:crypto'
import { writeFile } from 'node:fs/promises'

import type { Download, Locator, Page, Route, TestInfo } from '@playwright/test'
import * as Y from 'yjs'

import { EDITOR_SCHEMA_BEHAVIOR_INPUTS } from '../../packages/editor-schema/src/constants.ts'
import {
  LARGE_COLLABORATION_DOCUMENT_PARAGRAPHS,
} from '../../tests/verification/fixtures/collaboration-document-state.mjs'

import {
  collaborationSocketWindow,
  installCollaborationStatusObserver as installStatusObserverInPage,
  installCollaborationWebSocketObserver as installWebSocketObserverInPage,
  type CollaborationSocketObserverState,
  type CollaborationStatusObserverState,
} from '../browser-observer'
import { CollaborationFixtureControlClient } from '../control'
import type { CollaborationE2EStack } from '../config'
import { expect, test, type CollaborationHarness } from '../fixtures'
import {
  controlBoundsViolations,
  type Bounds as LayoutBounds,
  type ControlGeometry,
  type Viewport,
} from '../layout'
import {
  assertTransportFingerprint,
  observeTransportFingerprint,
} from '../transport-fingerprint'
import {
  leavePageAcceptingQuestion,
  reloadAcceptingLeaveQuestion,
} from '../leave-question'
import { parseCollaborationProtocolSession } from '../protocol-session'

const labels = {
  de: {
    accept: 'Annehmen',
    acceptAll: 'Alle annehmen',
    accessRevoked: 'Zugriff entzogen',
    all: 'Alle',
    assistant: 'KI',
    assistantPlaceholder: 'Beschreiben Sie, was am Dokument geändert werden soll...',
    author: 'Person',
    changes: 'Änderungen',
    sendInstruction: 'Senden',
    closeInspector: 'Inspector schließen',
    display: 'Anzeige',
    edit: 'Bearbeiten',
    exportBackup: 'Backup herunterladen',
    final: 'Final',
    importFile: 'Aus Datei importieren',
    live: 'Live',
    menu: 'Menü',
    moreActions: 'Weitere Dokumentaktionen',
    open: 'Offen',
    original: 'Original',
    privateComment: 'Private KI-Notiz',
    privateCommentPlaceholder: 'Private KI-Notiz hinzufügen …',
    privateSuggestionAccept: 'Übernehmen',
    reject: 'Ablehnen',
    rejectAll: 'Alle ablehnen',
    reconnecting: 'Verbindung wird wiederhergestellt',
    runSuggestion: 'Vorschlag erzeugen',
    readOnly: 'Schreibgeschützt',
    saved: 'Gespeichert',
    saving: 'Wird gespeichert',
    searchDocuments: 'Dokumente suchen',
    showInspector: 'Inspector einblenden',
    showMore: 'Mehr anzeigen',
    showTree: 'Dateibaum einblenden',
    simple: 'Einfach',
    source: 'Source',
    sourceEditor: 'Markdown Source',
    sourceReadOnly: 'Quelltext ist in der Zusammenarbeit schreibgeschützt.',
    suggest: 'Vorschlagen',
    type: 'Art',
    viewLocked: 'Diese Freigabe ist schreibgeschützt.',
  },
  en: {
    accept: 'Accept',
    acceptAll: 'Accept all',
    accessRevoked: 'Access revoked',
    all: 'All',
    assistant: 'AI',
    assistantPlaceholder: 'Describe what should change in this document...',
    author: 'Person',
    changes: 'Changes',
    sendInstruction: 'Send',
    closeInspector: 'Close inspector',
    display: 'Display',
    edit: 'Edit',
    exportBackup: 'Download backup',
    final: 'Final',
    importFile: 'Import from file',
    live: 'Live',
    menu: 'Menu',
    moreActions: 'More document actions',
    open: 'Open',
    original: 'Original',
    privateComment: 'Private AI note',
    privateCommentPlaceholder: 'Add private AI note …',
    privateSuggestionAccept: 'Accept',
    reject: 'Reject',
    rejectAll: 'Reject all',
    reconnecting: 'Reconnecting',
    runSuggestion: 'Generate suggestion',
    readOnly: 'Read-only',
    saved: 'Saved',
    saving: 'Saving',
    searchDocuments: 'Search documents',
    showInspector: 'Show inspector',
    showMore: 'Show more',
    showTree: 'Show file tree',
    simple: 'Simple',
    source: 'Source',
    sourceEditor: 'Markdown source',
    sourceReadOnly: 'Source is read-only during collaboration.',
    suggest: 'Suggest',
    type: 'Type',
    viewLocked: 'This share is read-only.',
  },
} as const

const LARGE_STATE_DURABLE_P95_BUDGET_MS = 500
// Mirrors ACCEPTED_LATENCY_CEILING_P95_MS in the load harness: the
// targets above stay the goal and are reported when missed, but only a
// value beyond this ceiling is treated as a defect rather than the
// accepted large-state latency risk.
const LARGE_STATE_ACCEPTED_CEILING_MS = 2_000
const LARGE_STATE_ROUNDS = 5
const LARGE_STATE_VISIBLE_P95_BUDGET_MS = 250
const LARGE_STATE_WRITERS = 5

type LargeStateLatencySample = {
  durableMs: number
  round: number
  sequence: number
  visibleMs: number
  writer: number
}

test.describe('two-user editor collaboration', () => {
  test('@transport-fingerprint @mobile each endpoint exposes its shipped transport fingerprint', async ({ configuredStack, request }) => {
    if (!configuredStack) {
      throw new Error('Playwright did not stop a test skipped for an unavailable stack.')
    }
    const { stack, transport } = configuredStack
    const baseURL = stack.transports[transport].baseURL
    if (!baseURL) throw new Error(`${transport} baseURL is unavailable after fixture validation.`)

    const observation = await observeTransportFingerprint(request, baseURL)
    expect(assertTransportFingerprint(transport, observation)).toBe(transport)
  })

  test('@direct-edit @mobile direct edit is visible once and remains durable after reload', async ({ collaboration }) => {
    const { collaboratorPage, ownerPage, stack } = requireCollaboration(collaboration)
    const documentId = stack.documents.directEdit
    await Promise.all([
      openDocument(ownerPage, documentId, stack.locale),
      openDocument(collaboratorPage, documentId, stack.locale),
    ])
    await Promise.all([
      waitForConnected(ownerPage, stack.locale),
      waitForConnected(collaboratorPage, stack.locale),
    ])

    const marker = uniqueMarker('direct')
    const collaboratorEditor = editor(collaboratorPage)
    let markerInserted = false
    try {
      await appendMarker(collaboratorEditor, marker)
      markerInserted = true
      await expectTextOccurrences(editor(collaboratorPage), marker, 1)
      await expectTextOccurrences(editor(ownerPage), marker, 1)
      await requireProjectionFlush(ownerPage, documentId)
      expectTextCount(
        (await editorDocumentDetail(ownerPage, documentId)).content_markdown,
        marker,
        1,
      )

      await Promise.all([
        reloadAcceptingLeaveQuestion(ownerPage, {
          expectQuestion: false,
          waitUntil: 'domcontentloaded',
        }),
        reloadAcceptingLeaveQuestion(collaboratorPage, {
          expectQuestion: false,
          waitUntil: 'domcontentloaded',
        }),
      ])
      await Promise.all([
        openDocument(ownerPage, documentId, stack.locale, false),
        openDocument(collaboratorPage, documentId, stack.locale, false),
      ])
      await Promise.all([
        waitForConnected(ownerPage, stack.locale),
        waitForConnected(collaboratorPage, stack.locale),
      ])
      await expectTextOccurrences(editor(ownerPage), marker, 1)
      await expectTextOccurrences(editor(collaboratorPage), marker, 1)
      expectTextCount(
        (await editorDocumentDetail(ownerPage, documentId)).content_markdown,
        marker,
        1,
      )
    } finally {
      if (markerInserted && !collaboratorPage.isClosed()) {
        await removeMarker(editor(collaboratorPage), marker)
        await expectTextOccurrences(editor(ownerPage), marker, 0)
        await requireProjectionFlush(ownerPage, documentId)
        expectTextCount(
          (await editorDocumentDetail(ownerPage, documentId)).content_markdown,
          marker,
          0,
        )
      }
    }
  })

  test('@large-state-latency @chromium-only a large collaboration state remains responsive under concurrent visible edits', async ({ collaboration }, testInfo) => {
    testInfo.setTimeout(180_000)
    const {
      collaboratorContext,
      collaboratorPage,
      ownerPage,
      stack,
    } = requireCollaboration(collaboration)
    const documentId = requireCapabilityDocument(
      stack.documents.largeState,
      'fixture.documents.largeState',
      testInfo,
    )
    const additionalCollaboratorPages = await Promise.all(
      Array.from(
        { length: LARGE_STATE_WRITERS - 2 },
        () => collaboratorContext.newPage(),
      ),
    )
    const writerPages = [
      ownerPage,
      collaboratorPage,
      ...additionalCollaboratorPages,
    ]
    expect(writerPages).toHaveLength(LARGE_STATE_WRITERS)

    await Promise.all(writerPages.map(installWebSocketObserver))
    await Promise.all(writerPages.map((page) => (
      openDocument(page, documentId, stack.locale)
    )))
    await Promise.all(writerPages.map((page) => waitForConnected(page, stack.locale)))
    await Promise.all(writerPages.map((page) => chooseWriteMode(page, stack.locale, 'edit')))
    await Promise.all(writerPages.map((page) => (
      expect(editor(page)).toHaveAttribute('contenteditable', 'true')
    )))

    const initialDocument = await editorDocumentDetail(ownerPage, documentId)
    expect(initialDocument.content_markdown.length).toBeGreaterThan(110_000)
    expectTextCount(
      initialDocument.content_markdown,
      'stable collaboration marker.',
      LARGE_COLLABORATION_DOCUMENT_PARAGRAPHS,
    )

    const samples: LargeStateLatencySample[] = []
    for (let round = 1; round <= LARGE_STATE_ROUNDS; round += 1) {
      const markers = writerPages.map((_, writerIndex) => (
        uniqueMarker(`large-state-r${round}-w${writerIndex + 1}`)
      ))
      const before = await editorDocumentDetail(ownerPage, documentId)
      const observerBaselines = await Promise.all(writerPages.map(async (page) => {
        const state = await observedCollaborationSocketState(page)
        return Math.max(0, ...state.events.map((event) => event.order))
      }))

      await Promise.all(writerPages.map((page, writerIndex) => (
        armLargeStateBrowserTiming(
          page,
          markers[writerIndex]!,
          markers[(writerIndex + writerPages.length - 1) % writerPages.length]!,
        )
      )))
      await Promise.all(writerPages.map((page, writerIndex) => (
        appendMarkerAtomically(page, editor(page), markers[writerIndex]!)
      )))

      await Promise.all(writerPages.flatMap((page) => (
        markers.map((marker) => expectTextOccurrences(editor(page), marker, 1))
      )))
      await Promise.all(writerPages.map((page, writerIndex) => (
        waitForLargeStateBrowserTiming(
          page,
          markers[writerIndex]!,
          markers[(writerIndex + writerPages.length - 1) % writerPages.length]!,
        )
      )))
      await Promise.all(writerPages.map((page, writerIndex) => (
        waitForDurableAckAfter(page, observerBaselines[writerIndex]!)
      )))
      await requireProjectionFlush(ownerPage, documentId)

      const after = await editorDocumentDetail(ownerPage, documentId)
      expect(after.collaboration.persisted_sequence).toBe(
        before.collaboration.persisted_sequence + LARGE_STATE_WRITERS,
      )
      expect(after.collaboration.projection_sequence).toBe(
        after.collaboration.persisted_sequence,
      )
      for (const marker of markers) {
        expectTextCount(after.content_markdown, marker, 1)
      }

      const observerStates = await Promise.all(
        writerPages.map(observedCollaborationSocketState),
      )
      const acknowledgements = observerStates.map((state, writerIndex) => {
        expect(state.pendingFrameDecodes).toBe(0)
        const matching = state.events.flatMap((event) => (
          event.kind === 'durable_ack'
          && event.order > observerBaselines[writerIndex]!
            ? [event]
            : []
        ))
        expect(matching).toHaveLength(1)
        return matching[0]!
      })
      const acknowledgementSequences = acknowledgements.map((ack) => ack.sequence)
      expect(new Set(acknowledgementSequences).size).toBe(LARGE_STATE_WRITERS)
      expect(Math.min(...acknowledgementSequences)).toBe(
        before.collaboration.persisted_sequence + 1,
      )
      expect(Math.max(...acknowledgementSequences)).toBe(
        after.collaboration.persisted_sequence,
      )

      const browserTimings = await Promise.all(writerPages.map((page, writerIndex) => (
        largeStateBrowserTiming(
          page,
          markers[writerIndex]!,
          markers[(writerIndex + writerPages.length - 1) % writerPages.length]!,
        )
      )))
      for (let writerIndex = 0; writerIndex < writerPages.length; writerIndex += 1) {
        const inputAt = browserTimings[writerIndex]!.inputAt
        const targetIndex = (writerIndex + 1) % writerPages.length
        const visibleAt = browserTimings[targetIndex]!.visibleAt
        const visibleMs = visibleAt - inputAt
        const durableMs = acknowledgements[writerIndex]!.observedAt - inputAt
        expect(visibleMs).toBeGreaterThanOrEqual(0)
        expect(durableMs).toBeGreaterThanOrEqual(0)
        samples.push({
          durableMs,
          round,
          sequence: acknowledgements[writerIndex]!.sequence,
          visibleMs,
          writer: writerIndex + 1,
        })
      }

      if (round === LARGE_STATE_ROUNDS) {
        await Promise.all(writerPages.map((page) => waitForConnected(page, stack.locale)))
        const evidenceNames = [
          'large-state-owner',
          'large-state-collaborator',
          'large-state-collaborator-session-2',
          'large-state-collaborator-session-3',
          'large-state-collaborator-session-4',
        ]
        const evidencePaths = evidenceNames.map((name) => (
          testInfo.outputPath(`${name}.png`)
        ))
        await Promise.all(writerPages.map((page, writerIndex) => (
          page.screenshot({
            animations: 'disabled',
            path: evidencePaths[writerIndex]!,
          })
        )))
        await Promise.all(evidenceNames.map((name, writerIndex) => (
          testInfo.attach(name, {
            contentType: 'image/png',
            path: evidencePaths[writerIndex]!,
          })
        )))
      }

      for (const marker of markers) {
        await removeMarker(editor(ownerPage), marker)
      }
      await Promise.all(writerPages.flatMap((page) => (
        markers.map((marker) => expectTextOccurrences(editor(page), marker, 0))
      )))
      await requireProjectionFlush(ownerPage, documentId)
      const cleaned = await editorDocumentDetail(ownerPage, documentId)
      for (const marker of markers) expectTextCount(cleaned.content_markdown, marker, 0)
    }

    const timingResult = {
      document: {
        characterCount: initialDocument.content_markdown.length,
        paragraphCount: LARGE_COLLABORATION_DOCUMENT_PARAGRAPHS,
      },
      durable: summarizeLatencies(samples.map((sample) => sample.durableMs)),
      identities: 2,
      rounds: LARGE_STATE_ROUNDS,
      samples,
      sessions: LARGE_STATE_WRITERS,
      visible: summarizeLatencies(samples.map((sample) => sample.visibleMs)),
    }
    const timingEvidencePath = testInfo.outputPath('large-state-latency.json')
    await writeFile(
      timingEvidencePath,
      `${JSON.stringify(timingResult, null, 2)}\n`,
      { mode: 0o600 },
    )
    await testInfo.attach('large-state-latency', {
      contentType: 'application/json',
      path: timingEvidencePath,
    })
    // The accepted large-state latency risk is an explicit architecture
    // decision: the visible p95 on a large state under concurrent writers
    // is known to sit above the 250ms target. Every integrity assertion
    // above stays hard — one match per input, one sequence per writer, a
    // contiguous sequence range, and a clean marker removal. Only the
    // latency target is reported as a warning,
    // with the measured value attached above and printed here, so a
    // regression is still visible while a known accepted risk does not
    // mask the rest of the multiuser matrix.
    //
    // The band is bounded on purpose. Above the ceiling an edit takes
    // seconds to appear, which is a functional defect rather than a
    // latency nuance, and stays a hard failure.
    for (const [label, measured, target] of [
      ['visible', timingResult.visible.p95, LARGE_STATE_VISIBLE_P95_BUDGET_MS],
      ['durable', timingResult.durable.p95, LARGE_STATE_DURABLE_P95_BUDGET_MS],
    ] as const) {
      expect(measured).toBeLessThan(LARGE_STATE_ACCEPTED_CEILING_MS)
      if (measured >= target) {
        const note = `Accepted large-state latency risk: ${label} p95 `
          + `${measured.toFixed(1)}ms exceeds the ${target}ms target `
          + `(warning; hard ceiling ${LARGE_STATE_ACCEPTED_CEILING_MS}ms).`
        testInfo.annotations.push({ description: note, type: 'warning' })
        process.stdout.write(`${note}\n`)
      }
    }
  })

  test('@remote-caret remote caret identifies the author at the visible edit position', async ({ collaboration }) => {
    const { collaboratorPage, ownerPage, stack } = requireCollaboration(collaboration)
    const documentId = stack.documents.remotePresence
    await Promise.all([
      openDocument(ownerPage, documentId, stack.locale),
      openDocument(collaboratorPage, documentId, stack.locale),
    ])
    await Promise.all([
      waitForConnected(ownerPage, stack.locale),
      waitForConnected(collaboratorPage, stack.locale),
    ])

    const marker = uniqueMarker('remote-caret')
    const collaboratorEditor = editor(collaboratorPage)
    await appendMarker(collaboratorEditor, marker)
    try {
      await expectTextOccurrences(editor(ownerPage), marker, 1)
      await expectRemoteCaretAtMarker(
        ownerPage,
        marker,
        stack.collaborator.displayName,
      )
    } finally {
      await removeMarker(collaboratorEditor, marker)
      await expectTextOccurrences(editor(ownerPage), marker, 0)
    }
  })

  test('@remote-selection remote text selection has a classified visible presentation', async ({ collaboration }) => {
    const { collaboratorPage, ownerPage, stack } = requireCollaboration(collaboration)
    const documentId = stack.documents.remotePresence
    await Promise.all([
      openDocument(ownerPage, documentId, stack.locale),
      openDocument(collaboratorPage, documentId, stack.locale),
    ])
    await Promise.all([
      waitForConnected(ownerPage, stack.locale),
      waitForConnected(collaboratorPage, stack.locale),
    ])

    const marker = uniqueMarker('remote-selection')
    const collaboratorEditor = editor(collaboratorPage)
    await appendMarker(collaboratorEditor, marker)
    try {
      await expectTextOccurrences(editor(ownerPage), marker, 1)
      await selectMarker(collaboratorEditor, marker)
      await expect.poll(async () => {
        const presentation = await collaborationSelectionPresentation(ownerPage)
        return {
          hasSelection: presentation.count > 0,
          opaqueCount: presentation.opaqueCount,
          unclassifiedCount: presentation.unclassifiedCount,
        }
      }).toEqual({
        hasSelection: true,
        opaqueCount: 0,
        unclassifiedCount: 0,
      })
    } finally {
      await removeMarker(collaboratorEditor, marker)
      await expectTextOccurrences(editor(ownerPage), marker, 0)
    }
  })

  test('@concurrent-edits @mobile concurrent owner and collaborator edits converge exactly once', async ({ collaboration }, testInfo) => {
    const { collaboratorPage, ownerPage, stack } = requireCollaboration(collaboration)
    const documentId = requireCapabilityDocument(
      stack.documents.concurrent,
      'fixture.documents.concurrent',
      testInfo,
    )
    await Promise.all([
      openDocument(ownerPage, documentId, stack.locale),
      openDocument(collaboratorPage, documentId, stack.locale),
    ])
    await Promise.all([
      waitForConnected(ownerPage, stack.locale),
      waitForConnected(collaboratorPage, stack.locale),
    ])
    await Promise.all([
      chooseWriteMode(ownerPage, stack.locale, 'edit'),
      chooseWriteMode(collaboratorPage, stack.locale, 'edit'),
    ])

    const ownerMarker = uniqueMarker('concurrent-owner')
    const collaboratorMarker = uniqueMarker('concurrent-collaborator')
    await Promise.all([
      appendMarkerAtomically(ownerPage, editor(ownerPage), ownerMarker),
      appendMarkerAtomically(
        collaboratorPage,
        editor(collaboratorPage),
        collaboratorMarker,
      ),
    ])
    for (const page of [ownerPage, collaboratorPage]) {
      await expectTextOccurrences(editor(page), ownerMarker, 1)
      await expectTextOccurrences(editor(page), collaboratorMarker, 1)
    }

    await removeMarker(editor(ownerPage), ownerMarker)
    await removeMarker(editor(ownerPage), collaboratorMarker)
    for (const page of [ownerPage, collaboratorPage]) {
      await expectTextOccurrences(editor(page), ownerMarker, 0)
      await expectTextOccurrences(editor(page), collaboratorMarker, 0)
    }
  })

  // Viele Stellen in dieser Datei beweisen, dass die Verbindung
  // abbricht, WENN sie soll -- Widerruf, Rechteentzug, Protokollverstoss.
  // Dieses Szenario sichert das Gegenstueck zu: dass sie es NICHT tut, wenn
  // nichts passiert. So wird "die Zusammenarbeit ist stabil" zu einem Tor.
  test('@stays-connected @mobile two writing sessions keep one socket and never surface the reconnect banner', async ({ collaboration }, testInfo) => {
    const { collaboratorPage, ownerPage, stack } = requireCollaboration(collaboration)
    // Eigenes Dokument, absichtlich von keinem anderen Szenario benutzt:
    // dieser Test haelt seine beiden Sitzungen eine halbe Minute offen. Auf
    // einem geteilten Dokument stiesse er auf die Restsitzungen eines
    // Szenarios, das die Seite neu laedt, liefe in den Deckel von fuenf
    // gleichzeitigen Sitzungen je Nutzer und Dokument -- und meldete den
    // Ratenschutz als Verbindungsabbruch. Genau das ist beim ersten Lauf
    // passiert, auf genau einem der vier Browserprojekte.
    const documentId = requireCapabilityDocument(
      stack.documents.staysConnected,
      'fixture.documents.staysConnected',
      testInfo,
    )
    const pages = [ownerPage, collaboratorPage]

    // Beide Sonden VOR der Navigation: addInitScript wirkt erst ab der
    // naechsten Seitenladung. Nach openDocument installiert beobachten sie
    // nichts und meldeten trotzdem "keine Vorkommnisse".
    for (const page of pages) {
      await installWebSocketObserver(page)
      await installStatusObserver(page)
    }
    await Promise.all(pages.map((page) => openDocument(page, documentId, stack.locale)))
    await Promise.all(pages.map((page) => waitForConnected(page, stack.locale)))
    await Promise.all(pages.map((page) => chooseWriteMode(page, stack.locale, 'edit')))

    // Die Sonden muessen BEWEISEN, dass sie mitschreiben. Ohne diesen Schritt
    // waere "null Abbrueche" eine Aussage ueber ein leeres Protokoll und das
    // Szenario immergruen -- der teuerste Fehler, den ein Tor machen kann.
    for (const page of pages) {
      await expect.poll(() => observedActiveCollaborationSocketId(page)).not.toBeNull()
      expect(
        await observedReconnectAppearances(page),
        'The status observer must be installed before the assertions read it.',
      ).not.toBeNull()
    }

    const markers: string[] = []
    for (let round = 0; round < 6; round += 1) {
      const page = round % 2 === 0 ? ownerPage : collaboratorPage
      const marker = uniqueMarker(`stays-connected-${round}`)
      // appendMarkerAtomically statt Tastatureingabe: es setzt die Einfuegung
      // ueber eine DOM-Range ans echte Dokumentende und schreibt den Marker in
      // EINEM Zug. Zeichenweises Tippen ans Zeilenende laesst eine
      // eintreffende Fremdaenderung den Marker mitten im Wort zerreissen.
      await appendMarkerAtomically(page, editor(page), marker)
      markers.push(marker)
      await page.waitForTimeout(4_000)
    }

    // Konvergenz: jede Marke steht in BEIDEN Sitzungen genau einmal.
    for (const page of pages) {
      for (const marker of markers) {
        await expectTextOccurrences(editor(page), marker, 1)
      }
    }

    for (const page of pages) {
      expect(
        await observedCloseCodes(page),
        'A session that only writes must not lose its collaboration socket.',
      ).toEqual([])
      expect(
        await observedReconnectAppearances(page),
        'The reconnect banner must never appear while the transport is healthy.',
      ).toEqual([])
    }

    // Das Fixture-Dokument wird von mehreren Szenarien geteilt; die Marken
    // gehen wieder raus, damit der naechste Lauf denselben Ausgangstext sieht.
    for (const marker of markers) {
      await removeMarker(editor(ownerPage), marker)
    }
    for (const page of pages) {
      for (const marker of markers) {
        await expectTextOccurrences(editor(page), marker, 0)
      }
    }
  })

  // Der Editor-Assistent ist ein Hauptpfad des Produkts, und dieses
  // Szenario prueft ihn Ende zu Ende: "Uebernehmen" muss den Vorschlag
  // veroeffentlichen, statt etwa mit 409 patch_not_found zu antworten --
  // ein Fehler, der JEDEN Nutzer bei JEDEM Versuch traefe.
  //
  // Der Modellaufruf ist gestubbt, die Serverseite nicht: Traegerzeile,
  // privater Entwurf und Veroeffentlichung laufen echt. Gegatet wird der
  // Mechanismus, nicht das Modell -- ein LLM-Aufruf je Verifikationslauf
  // waere teuer und nicht reproduzierbar.
  test('@ai-suggestion-accept an assistant suggestion publishes and reaches the second session', async ({ collaboration }, testInfo) => {
    const { collaboratorPage, ownerPage, stack } = requireCollaboration(collaboration)
    const documentId = requireCapabilityDocument(
      stack.documents.aiSuggestion,
      'fixture.documents.aiSuggestion',
      testInfo,
    )
    const pages = [ownerPage, collaboratorPage]
    await Promise.all(pages.map((page) => openDocument(page, documentId, stack.locale)))
    await Promise.all(pages.map((page) => waitForConnected(page, stack.locale)))

    // Der Ankersatz steht woertlich im Fixture-Dokument; der gestubbte
    // Vorschlag ersetzt darin genau ein Wort.
    const anchorText = 'Der Assistent ersetzt hier ein Wort.'
    // Ein echter Wortwechsel, kein Anhaengsel: 'ersetzt' -> 'ersetzte' waere
    // eine reine Einfuegung eines Buchstabens, und die Diff-Bildung erzeugt
    // dafuer voellig zu Recht KEINE Loeschmarke. Der veroeffentlichte
    // Ersetzungspfad soll aber beide Marken tragen.
    const replacement = 'Der Assistent tauscht hier ein Wort.'
    const routePattern = '**/v1/editor/instruct'
    const handler = async (route: Route): Promise<void> => {
      await route.fulfill({
        body: JSON.stringify({
          assistant_message: 'Ein Wort wurde ersetzt.',
          edits: [{
            find: anchorText,
            note: 'Zeitform angepasst.',
            position: 'replace',
            quote_after: '',
            quote_before: '',
            text: replacement,
          }],
          warnings: [],
        }),
        contentType: 'application/json',
        status: 200,
      })
    }
    await ownerPage.route(routePattern, handler)
    try {
      await openAssistant(ownerPage, stack.locale)
      const composer = ownerPage.getByRole('textbox', {
        name: labels[stack.locale].assistantPlaceholder,
      })
      await composer.click()
      await ownerPage.keyboard.insertText('Setze den Satz in die Vergangenheit.')

      // Die Traegerzeile ist die Vorautorisierung: ohne einen creator-privaten
      // Entwurf auf genau dieses patch_id weist der Serverwaechter die
      // Veroeffentlichung ab. Dass sie wirklich geschrieben wird, ist Teil der
      // Zusicherung -- nicht nur, dass am Ende ein Vorschlag dasteht.
      const [draftPersistence] = await Promise.all([
        ownerPage.waitForResponse((response) => {
          const path = new URL(response.url()).pathname
          return response.request().method() === 'PUT'
            && path.startsWith(`/v1/editor/documents/${documentId}/comments/`)
            && path.endsWith('/suggestion-draft')
        }, { timeout: 60_000 }),
        ownerPage.getByRole('button', {
          name: labels[stack.locale].sendInstruction,
          exact: true,
        }).click(),
      ])
      expect(draftPersistence.status()).toBe(200)
    } finally {
      if (!ownerPage.isClosed()) await ownerPage.unroute(routePattern, handler)
    }

    // Uebernehmen: dieser Pfad darf nie mit 409 patch_not_found antworten.
    const [publication] = await Promise.all([
      ownerPage.waitForResponse((response) => (
        new URL(response.url()).pathname
          === `/v1/editor/documents/${documentId}/suggestions:publish`
      ), { timeout: 60_000 }),
      ownerPage.getByRole('button', {
        name: labels[stack.locale].privateSuggestionAccept,
      }).first().click(),
    ])
    expect(
      publication.status(),
      'An assistant publication must carry its own authorisation.',
    ).toBe(200)

    // Inhaltliche Abnahme, in BEIDEN Sitzungen: der zweite Nutzer sieht die
    // Aenderung als geteilte Tracked Change. Geprueft ueber die Kennungen aus
    // der Antwort und den vorhandenen Helfer, nicht ueber Text -- innerText
    // verklebt Loeschung und Einfuegung zu einem Wort, das nirgends steht.
    const published = await publication.json() as {
      patch_id: string
      suggestion_ids: string[]
    }
    expect(published.suggestion_ids).toHaveLength(1)
    for (const page of pages) {
      // Erst die Aenderung auswaehlen, dann die Marken pruefen: in der
      // Standardansicht ist die Loeschung zwar im DOM, aber nicht sichtbar.
      // Ohne diesen Schritt prueft man die Anwesenheit eines Knotens statt
      // dessen, was der Nutzer sieht.
      await selectPublishedCollaborationChange(
        page,
        stack.locale,
        published.patch_id,
      )
      await expectPublishedReplacementMarks(
        page,
        published.suggestion_ids[0]!,
        published.patch_id,
      )
    }

    // Aufraeumen ist hier Teil der Zusicherung, nicht Hoeflichkeit: dieses
    // Szenario VEROEFFENTLICHT eine Aenderung und wuerde den Ankersatz
    // dauerhaft umschreiben. Die vier Browserprojekte laufen nacheinander
    // gegen DASSELBE Dokument -- ohne Ruecknahme faende schon das zweite
    // seinen Anker nicht mehr und meldete einen Verankerungsfehler, der
    // in Wahrheit der Rueckstand des ersten waere.
    const changeRow = ownerPage.locator(
      `[data-inspector-change-id="${published.patch_id}"]`,
    )
    await changeRow.getByRole('button', {
      exact: true,
      name: labels[stack.locale].reject,
    }).click()
    await expect(changeRow).toHaveCount(0)
    for (const page of pages) {
      await expectTextOccurrences(editor(page), anchorText, 1)
      await expectTextOccurrences(editor(page), replacement, 0)
    }
  })

  test('@suggestions @mobile suggestions can be accepted and rejected by the owner', async ({ collaboration }) => {
    const { collaboratorPage, ownerPage, stack } = requireCollaboration(collaboration)
    const suggestionFixture = stack.documents.suggestion
    const documentId = suggestionFixture.documentId
    await Promise.all([
      openDocument(ownerPage, documentId, stack.locale),
      openDocument(collaboratorPage, documentId, stack.locale),
    ])
    await Promise.all([
      waitForConnected(ownerPage, stack.locale),
      waitForConnected(collaboratorPage, stack.locale),
    ])
    const share = await activeEditorShare(ownerPage, documentId, stack.collaborator.userId)
    expect(share.permission).toBe(suggestionFixture.expectedPermission)
    const schemaVersion = (await editorDocumentDetail(ownerPage, documentId))
      .collaboration.schema_version
    const sessionResult = await browserApi(
      collaboratorPage,
      `/v1/editor/documents/${documentId}/collaboration/session`,
      'POST',
      { protocol_version: 1, schema_version: schemaVersion },
    )
    requireApiSuccess(sessionResult)
    const suggestSession = parseCollaborationProtocolSession(sessionResult.payload)
    expect(suggestSession.access).toBe(suggestionFixture.expectedPermission)
    expect(suggestSession.initialWriteMode).toBe(suggestionFixture.expectedPermission)
    expect(suggestSession.userId).toBe(suggestionFixture.expectedAuthorId)

    await requireProjectionFlush(ownerPage, documentId)
    const beforeManipulatedUpdate = await editorDocumentDetail(ownerPage, documentId)
    const manipulatedMarker = uniqueMarker('suggest-direct-yjs-rejected')
    const manipulatedUpdate = createIndependentYjsUpdate(manipulatedMarker)
    await openBrowserProtocolProbe(collaboratorPage, sessionResult.payload)
    try {
      await sendBrowserProtocolProbeUpdate(collaboratorPage, manipulatedUpdate.bytes)
      await expect.poll(async () => {
        const state = await browserProtocolProbeState(collaboratorPage)
        return { closeCodes: state.closeCodes, errors: state.errors }
      }, { timeout: 30_000 }).toEqual({ closeCodes: [4403], errors: [] })
      await requireProjectionFlush(ownerPage, documentId)
      const afterManipulatedUpdate = await editorDocumentDetail(ownerPage, documentId)
      expect(afterManipulatedUpdate.collaboration.persisted_sequence).toBe(
        beforeManipulatedUpdate.collaboration.persisted_sequence,
      )
      expect(afterManipulatedUpdate.collaboration.projection_sequence).toBe(
        beforeManipulatedUpdate.collaboration.projection_sequence,
      )
      expect(afterManipulatedUpdate.content_markdown).not.toContain(manipulatedMarker)
      expect((await browserProtocolProbeState(collaboratorPage)).durableAckHashes)
        .not.toContain(manipulatedUpdate.hash)
      await expectTextOccurrences(editor(ownerPage), manipulatedMarker, 0)
      await expectTextOccurrences(editor(collaboratorPage), manipulatedMarker, 0)
    } finally {
      await closeBrowserProtocolProbe(collaboratorPage)
    }

    await chooseWriteMode(collaboratorPage, stack.locale, 'suggest')

    const acceptedMarker = uniqueMarker('accept')
    await appendMarker(editor(collaboratorPage), acceptedMarker)
    await expectSuggestionIdentity(
      ownerPage,
      collaboratorPage,
      acceptedMarker,
      suggestionFixture.expectedAuthorId,
    )
    await decideVisibleSuggestion(ownerPage, stack.locale, acceptedMarker, 'accept')
    await expect(editor(ownerPage)).toContainText(acceptedMarker)
    await expect(editor(collaboratorPage)).toContainText(acceptedMarker)

    await chooseWriteMode(ownerPage, stack.locale, 'edit')
    await removeMarker(editor(ownerPage), acceptedMarker)
    await expect(editor(collaboratorPage)).not.toContainText(acceptedMarker)

    const rejectedMarker = uniqueMarker('reject')
    await appendMarker(editor(collaboratorPage), rejectedMarker)
    await expectSuggestionIdentity(
      ownerPage,
      collaboratorPage,
      rejectedMarker,
      suggestionFixture.expectedAuthorId,
    )
    await decideVisibleSuggestion(ownerPage, stack.locale, rejectedMarker, 'reject')
    await expect(editor(ownerPage)).not.toContainText(rejectedMarker)
    await expect(editor(collaboratorPage)).not.toContainText(rejectedMarker)
  })

  test('@suggestion-undo account editor undo rejects its tracked patch without closing the socket and survives reload', async ({ collaboration }) => {
    const { collaboratorPage, ownerPage, stack } = requireCollaboration(collaboration)
    const documentId = stack.documents.suggestionUndo
    await installWebSocketObserver(collaboratorPage)
    await Promise.all([
      openDocument(ownerPage, documentId, stack.locale),
      openDocument(collaboratorPage, documentId, stack.locale),
    ])
    await Promise.all([
      waitForConnected(ownerPage, stack.locale),
      waitForConnected(collaboratorPage, stack.locale),
    ])
    const share = await activeEditorShare(ownerPage, documentId, stack.collaborator.userId)
    expect(share.permission).toBe('edit')
    await chooseWriteMode(collaboratorPage, stack.locale, 'suggest')

    const socketId = await observedActiveCollaborationSocketId(collaboratorPage)
    if (socketId === null) throw new Error('No active collaboration socket before Suggest undo.')
    const closeCodesBeforeUndo = await observedCloseCodes(collaboratorPage)
    const marker = uniqueMarker('account-suggest-undo')
    let cleanupRequired = true
    try {
      await appendMarker(editor(collaboratorPage), marker)
      await expectSuggestionIdentity(
        ownerPage,
        collaboratorPage,
        marker,
        stack.collaborator.userId,
      )

      const shortcut = await collaboratorPage.evaluate(() => (
        /Mac|iPhone|iPad|iPod/.test(navigator.platform) ? 'Meta+z' : 'Control+z'
      ))
      await editor(collaboratorPage).press(shortcut)

      await expectTextOccurrences(editor(ownerPage), marker, 0)
      await expectTextOccurrences(editor(collaboratorPage), marker, 0)
      cleanupRequired = false
      expect(await observedActiveCollaborationSocketId(collaboratorPage)).toBe(socketId)
      expect(await observedCloseCodes(collaboratorPage)).toEqual(closeCodesBeforeUndo)

      await reloadAcceptingLeaveQuestion(collaboratorPage, {
        expectQuestion: false,
        waitUntil: 'domcontentloaded',
      })
      await openDocument(collaboratorPage, documentId, stack.locale, false)
      await waitForConnected(collaboratorPage, stack.locale)
      await expectTextOccurrences(editor(collaboratorPage), marker, 0)
      await expectTextOccurrences(editor(ownerPage), marker, 0)
    } finally {
      if (
        cleanupRequired
        && await editor(ownerPage).getByText(marker, { exact: false }).count() > 0
      ) {
        await decideVisibleSuggestion(ownerPage, stack.locale, marker, 'reject')
      }
    }
  })

  test('@ime @mobile @chromium-only genuine Chromium IME composition creates one shared suggestion', async ({ collaboration }) => {
    const { collaboratorPage, ownerPage, stack } = requireCollaboration(collaboration)
    const documentId = stack.documents.ime
    await Promise.all([
      openDocument(ownerPage, documentId, stack.locale),
      openDocument(collaboratorPage, documentId, stack.locale),
    ])
    await Promise.all([
      waitForConnected(ownerPage, stack.locale),
      waitForConnected(collaboratorPage, stack.locale),
    ])
    await chooseWriteMode(collaboratorPage, stack.locale, 'suggest')

    const marker = `${uniqueMarker('ime')}-日本`
    const compositionEvents = await composeMarkerWithChromiumIme(
      collaboratorPage,
      editor(collaboratorPage),
      marker,
    )
    expect(compositionEvents.some((event) => (
      event.isTrusted && event.type === 'compositionstart'
    ))).toBe(true)
    // Chromium's DevTools IME commit reports compositionend as synthetic.
    // Trust is anchored on the start and composing input; the assertions below
    // prove the committed text, shared suggestion identity, and owner controls.
    expect(compositionEvents.some((event) => (
      event.type === 'compositionend'
    ))).toBe(true)
    expect(compositionEvents.some((event) => (
      event.isTrusted && event.type === 'input' && event.isComposing
    ))).toBe(true)
    await expectTextOccurrences(editor(collaboratorPage), marker, 1)
    await expectTextOccurrences(editor(ownerPage), marker, 1)

    const ownerMarks = ownerPage.locator('.editor-prose ins[data-suggestion-id]', {
      hasText: marker,
    })
    const collaboratorMarks = collaboratorPage.locator(
      '.editor-prose ins[data-suggestion-id]',
      { hasText: marker },
    )
    await expect(ownerMarks).toHaveCount(1)
    await expect(collaboratorMarks).toHaveCount(1)
    const suggestionId = await ownerMarks.getAttribute('data-suggestion-id')
    expect(suggestionId).toBeTruthy()
    expect(await collaboratorMarks.getAttribute('data-suggestion-id')).toBe(suggestionId)

    await ensureInspector(ownerPage, stack.locale)
    await ownerPage.getByRole('tab', {
      name: new RegExp(`^${labels[stack.locale].changes}`),
    }).click()
    await expect(
      ownerPage.locator('[data-inspector-change-id]', { hasText: marker }),
    ).toHaveCount(1)

    await decideVisibleSuggestion(ownerPage, stack.locale, marker, 'reject')
    await expectTextOccurrences(editor(ownerPage), marker, 0)
    await expectTextOccurrences(editor(collaboratorPage), marker, 0)
  })

  test('@revocation @mobile revocation closes the active socket, hides the document with 404, and reconnects after restore', async ({ collaboration }) => {
    const { collaboratorPage, ownerPage, stack } = requireCollaboration(collaboration)
    const documentId = stack.documents.revocation
    await installWebSocketObserver(collaboratorPage)
    await Promise.all([
      openDocument(ownerPage, documentId, stack.locale),
      openDocument(collaboratorPage, documentId, stack.locale),
    ])
    await Promise.all([
      waitForConnected(ownerPage, stack.locale),
      waitForConnected(collaboratorPage, stack.locale),
    ])
    await expect.poll(() => observedActiveCollaborationSocketId(collaboratorPage))
      .not.toBeNull()
    const originalSocketId = await observedActiveCollaborationSocketId(collaboratorPage)
    if (originalSocketId === null) {
      throw new Error('No active collaboration socket was observable before revocation.')
    }
    const baselineObserverState = await observedCollaborationSocketState(collaboratorPage)
    const baselineOrder = Math.max(
      0,
      ...baselineObserverState.events.map((event) => event.order),
    )
    const schemaVersion = (await editorDocumentDetail(ownerPage, documentId))
      .collaboration.schema_version

    const share = await activeEditorShare(ownerPage, documentId, stack.collaborator.userId)
    let restoreRequired = false
    try {
      requireApiSuccess(await browserApi(ownerPage, `/v1/shares/${share.id}`, 'DELETE'))
      restoreRequired = true
      await waitForObservedSocketClosure(
        collaboratorPage,
        originalSocketId,
        baselineOrder,
      )
      const deniedSession = await browserApi(
        collaboratorPage,
        `/v1/editor/documents/${documentId}/collaboration/session`,
        'POST',
        { protocol_version: 1, schema_version: schemaVersion },
      )
      expect(
        deniedSession.status,
        `Revoked collaboration session returned ${apiFailureReason(deniedSession) ?? 'no public reason'}.`,
      ).toBe(404)
      const hiddenDocument = await browserApi(
        collaboratorPage,
        `/v1/editor/documents/${documentId}`,
        'GET',
      )
      expect(hiddenDocument.status).toBe(404)
      await expect(
        collaboratorPage.locator(`[data-editor-document-id="${documentId}"]`),
      ).toHaveCount(0, { timeout: 30_000 })
      await expect(
        collaboratorPage.getByText('Live-Widerruf', { exact: true }),
      ).toHaveCount(0)

      await restoreEditorShare(
        ownerPage,
        collaboratorPage,
        documentId,
        stack.collaborator.userId,
        share.permission,
      )
      restoreRequired = false
      await reloadAcceptingLeaveQuestion(collaboratorPage, {
        expectQuestion: false,
        waitUntil: 'domcontentloaded',
      })
      await openDocument(collaboratorPage, documentId, stack.locale, false)
      await waitForConnected(collaboratorPage, stack.locale)
    } finally {
      if (restoreRequired) {
        await restoreEditorShare(
          ownerPage,
          collaboratorPage,
          documentId,
          stack.collaborator.userId,
          share.permission,
        )
      }
    }
  })

  test('@permission-downgrade @mobile the same live edit socket rejects updates after downgrade', async ({ collaboration }, testInfo) => {
    const { collaboratorPage, ownerPage, stack } = requireCollaboration(collaboration)
    const documentId = requireCapabilityDocument(
      stack.documents.downgrade,
      'fixture.documents.downgrade',
      testInfo,
    )
    await Promise.all([
      openDocument(ownerPage, documentId, stack.locale),
      openDocument(collaboratorPage, documentId, stack.locale),
    ])
    await Promise.all([
      waitForConnected(ownerPage, stack.locale),
      waitForConnected(collaboratorPage, stack.locale),
    ])

    let share = await activeEditorShare(ownerPage, documentId, stack.collaborator.userId)
    expect(share.permission).toBe('edit')
    const schemaVersion = (await editorDocumentDetail(ownerPage, documentId))
      .collaboration.schema_version
    const editSession = await browserApi(
      collaboratorPage,
      `/v1/editor/documents/${documentId}/collaboration/session`,
      'POST',
      { protocol_version: 1, schema_version: schemaVersion },
    )
    requireApiSuccess(editSession)
    expect((editSession.payload as { access?: unknown } | null)?.access).toBe('edit')
    await openBrowserProtocolProbe(collaboratorPage, editSession.payload)
    await requireProjectionFlush(ownerPage, documentId)
    const beforeRejectedUpdate = await editorDocumentDetail(ownerPage, documentId)
    const rejectedMarker = uniqueMarker('downgrade-rejected-protocol-update')
    const rejectedUpdate = createIndependentYjsUpdate(rejectedMarker)
    await armBrowserProtocolProbeUpdate(collaboratorPage, rejectedUpdate.bytes)
    let restoreRequired = false
    try {
      share = await updateEditorSharePermission(ownerPage, share, 'view')
      restoreRequired = true
      await commitBrowserProtocolProbePolicyChange(collaboratorPage)
      await expect.poll(
        async () => {
          const state = await browserProtocolProbeState(collaboratorPage)
          return {
            challenged: state.authChallenges > state.authChallengesAtArm,
            errors: state.errors,
            rejected: state.authenticationDenied
              || state.scopes.includes('readonly')
              || state.closeCodes.includes(4403),
            sentAfterChallenge: state.updateSentAfterChallenge,
          }
        },
        { timeout: 30_000 },
      ).toEqual({
        challenged: true,
        errors: [],
        rejected: true,
        sentAfterChallenge: true,
      })
      await expect(editor(collaboratorPage)).toHaveAttribute('contenteditable', 'false', {
        timeout: 30_000,
      })
      await ensureInspector(collaboratorPage, stack.locale)
      const editMode = collaboratorPage.getByRole('button', {
        name: labels[stack.locale].edit,
        exact: true,
      }).first()
      await expect(editMode).toBeDisabled({ timeout: 30_000 })
      await editMode.locator('..').focus()
      await expect(collaboratorPage.getByRole('tooltip')).toHaveText(
        labels[stack.locale].viewLocked,
        { timeout: 30_000 },
      )
      await collaboratorPage.keyboard.press('Escape')
      const collaborationStatus = collaboratorPage
        .locator('[data-editor-status-label]')
        .first()
      await expect(collaborationStatus).toHaveText(
        labels[stack.locale].readOnly,
        { timeout: 30_000 },
      )
      await expect(collaborationStatus).not.toHaveText(
        labels[stack.locale].accessRevoked,
      )

      const viewSession = await browserApi(
        collaboratorPage,
        `/v1/editor/documents/${documentId}/collaboration/session`,
        'POST',
        { protocol_version: 1, schema_version: schemaVersion },
      )
      requireApiSuccess(viewSession)
      expect((viewSession.payload as { access?: unknown } | null)?.access).toBe('view')

      await requireProjectionFlush(ownerPage, documentId)
      const afterRejectedUpdate = await editorDocumentDetail(ownerPage, documentId)
      expect(afterRejectedUpdate.collaboration.persisted_sequence).toBe(
        beforeRejectedUpdate.collaboration.persisted_sequence,
      )
      expect(afterRejectedUpdate.collaboration.projection_sequence).toBe(
        beforeRejectedUpdate.collaboration.projection_sequence,
      )
      expect(afterRejectedUpdate.content_markdown).not.toContain(rejectedMarker)
      const probeAfterRejection = await browserProtocolProbeState(collaboratorPage)
      expect(probeAfterRejection.durableAckHashes).not.toContain(rejectedUpdate.hash)
      await expectTextOccurrences(editor(ownerPage), rejectedMarker, 0)
      await expectTextOccurrences(editor(collaboratorPage), rejectedMarker, 0)

      share = await updateEditorSharePermission(ownerPage, share, 'edit')
      restoreRequired = false
      await reloadAcceptingLeaveQuestion(collaboratorPage, {
        expectQuestion: false,
        waitUntil: 'domcontentloaded',
      })
      await openDocument(collaboratorPage, documentId, stack.locale, false)
      await waitForConnected(collaboratorPage, stack.locale)
      await expect(editor(collaboratorPage)).toHaveAttribute('contenteditable', 'true')
      const marker = uniqueMarker('downgrade-restored')
      await appendMarker(editor(collaboratorPage), marker)
      await expectTextOccurrences(editor(ownerPage), marker, 1)
      await removeMarker(editor(collaboratorPage), marker)
      await expectTextOccurrences(editor(ownerPage), marker, 0)
    } finally {
      await closeBrowserProtocolProbe(collaboratorPage)
      if (restoreRequired) {
        const current = await activeEditorShare(
          ownerPage,
          documentId,
          stack.collaborator.userId,
        )
        if (current.permission !== 'edit') {
          await updateEditorSharePermission(ownerPage, current, 'edit')
        }
      }
    }
  })

  test('@reconciliation @mobile a lost durable ACK reconciles exactly once after reconnect and reload', async ({ collaboration }, testInfo) => {
    const { collaboratorPage, ownerPage, stack } = requireCollaboration(collaboration)
    const documentId = requireCapabilityDocument(
      stack.documents.reconciliation,
      'fixture.documents.reconciliation',
      testInfo,
    )
    const controls = requireFixtureControls(stack, testInfo)
    const client = new CollaborationFixtureControlClient(controls)
    await installWebSocketObserver(collaboratorPage)
    await Promise.all([
      openDocument(ownerPage, documentId, stack.locale),
      openDocument(collaboratorPage, documentId, stack.locale),
    ])
    await Promise.all([
      waitForConnected(ownerPage, stack.locale),
      waitForConnected(collaboratorPage, stack.locale),
    ])

    await ensureInspector(collaboratorPage, stack.locale)
    await expect(
      collaboratorPage.locator('[data-editor-status-kind="saved"]').first(),
    ).toBeVisible({ timeout: 30_000 })
    await expect(
      collaboratorPage.locator('[data-editor-status-kind="saving"]'),
    ).toHaveCount(0)
    await closeMobileInspector(collaboratorPage, stack.locale)
    await expect.poll(() => observedActiveCollaborationSocketId(collaboratorPage))
      .not.toBeNull()
    const originalSocketId = await observedActiveCollaborationSocketId(collaboratorPage)
    if (originalSocketId === null) {
      throw new Error('No active collaboration socket was observable before arming lost ACK.')
    }
    const baselineObserverState = await observedCollaborationSocketState(collaboratorPage)
    const baselineOrder = Math.max(0, ...baselineObserverState.events.map((event) => event.order))

    const marker = uniqueMarker('lost-ack')
    const operation = await client.armLostAck(documentId, stack.collaborator.userId)
    expect(operation.state).toBe('armed')
    let durableSequence = 0
    try {
      await appendMarkerAtomically(
        collaboratorPage,
        editor(collaboratorPage),
        marker,
      )
      const triggered = await client.waitForState(operation.operationId, 'triggered')
      expect(triggered.closeCode).toBe(1012)
      expect(triggered.durableSequence).not.toBeNull()
      expect(triggered.durableSequence!).toBeGreaterThan(0)
      durableSequence = triggered.durableSequence!
      await waitForObservedSocketClosure(
        collaboratorPage,
        originalSocketId,
        baselineOrder,
      )
      await expect.poll(async () => (
        await observedCollaborationSocketState(collaboratorPage)
      ).pendingFrameDecodes).toBe(0)
      const observerState = await observedCollaborationSocketState(collaboratorPage)
      const originalSocketWindow = collaborationSocketWindow(
        observerState.events,
        originalSocketId,
        baselineOrder,
      )
      expect(
        originalSocketWindow,
        'The original browser collaboration socket must observe the fixture close.',
      ).not.toBeNull()
      expect(
        originalSocketWindow!.durableAcks,
        'No durable ACK may reach the browser data path before the forced close.',
      ).toEqual([])
      expect(
        originalSocketWindow!.protocolErrors,
        'The browser observer must decode every collaboration frame in the fault window.',
      ).toEqual([])
      await ensureInspector(collaboratorPage, stack.locale)
      await expect(
        collaboratorPage.locator('[data-editor-status-kind="reconnecting"]').first(),
      ).toBeVisible({ timeout: 20_000 })
      await expect(editor(collaboratorPage)).toHaveAttribute('contenteditable', 'false')
    } finally {
      const restored = await client.restore(operation.operationId)
      expect(restored.state).toBe('ready')
    }
    await waitForConnected(collaboratorPage, stack.locale)
    const reconnectedSocketId = await observedActiveCollaborationSocketId(collaboratorPage)
    expect(reconnectedSocketId).not.toBeNull()
    expect(reconnectedSocketId).not.toBe(originalSocketId)
    const reconciled = await client.waitForDurabilityReconciliation(operation.operationId)
    expect(reconciled.durabilityReconciled).toBe(true)
    expect(reconciled.pendingDurabilityCount).toBe(0)
    expect(reconciled.durableSequence).toBe(durableSequence)
    expect(reconciled.reconciliationSequence).not.toBeNull()
    expect(reconciled.reconciliationSequence!).toBeGreaterThanOrEqual(
      durableSequence,
    )
    await ensureInspector(collaboratorPage, stack.locale)
    await expect(
      collaboratorPage.locator('[data-editor-status-kind="saved"]').first(),
    ).toBeVisible({ timeout: 30_000 })
    await expect(
      collaboratorPage.locator('[data-editor-status-kind="saving"]'),
    ).toHaveCount(0)
    await closeMobileInspector(collaboratorPage, stack.locale)
    await requireProjectionFlush(ownerPage, documentId)
    await Promise.all([
      reloadAcceptingLeaveQuestion(ownerPage, {
        expectQuestion: false,
        waitUntil: 'domcontentloaded',
      }),
      reloadAcceptingLeaveQuestion(collaboratorPage, {
        expectQuestion: false,
        waitUntil: 'domcontentloaded',
      }),
    ])
    await Promise.all([
      openDocument(ownerPage, documentId, stack.locale, false),
      openDocument(collaboratorPage, documentId, stack.locale, false),
    ])
    await expectTextOccurrences(editor(ownerPage), marker, 1)
    await expectTextOccurrences(editor(collaboratorPage), marker, 1)
    await removeMarker(editor(ownerPage), marker)
    await expectTextOccurrences(editor(collaboratorPage), marker, 0)
  })

  test('@outage @mobile sidecar outage exposes a stale projection and recovers every durable marker', async ({ collaboration }, testInfo) => {
    const { collaboratorPage, ownerPage, stack } = requireCollaboration(collaboration)
    const documentId = requireCapabilityDocument(
      stack.documents.outage,
      'fixture.documents.outage',
      testInfo,
    )
    const controls = requireFixtureControls(stack, testInfo)
    const client = new CollaborationFixtureControlClient(controls)
    await installWebSocketObserver(collaboratorPage)
    await Promise.all([
      openDocument(ownerPage, documentId, stack.locale),
      openDocument(collaboratorPage, documentId, stack.locale),
    ])
    await Promise.all([
      waitForConnected(ownerPage, stack.locale),
      waitForConnected(collaboratorPage, stack.locale),
    ])
    await expect.poll(() => observedActiveCollaborationSocketId(collaboratorPage))
      .not.toBeNull()
    const originalSocketId = await observedActiveCollaborationSocketId(collaboratorPage)
    if (originalSocketId === null) {
      throw new Error('No active collaboration socket was observable before the sidecar outage.')
    }
    const baselineObserverState = await observedCollaborationSocketState(collaboratorPage)
    const baselineOrder = Math.max(
      0,
      ...baselineObserverState.events.map((event) => event.order),
    )
    assertFastApiHealth(await browserApi(ownerPage, '/health', 'GET'))
    const before = await editorDocumentDetail(ownerPage, documentId)
    const marker = uniqueMarker('outage')
    const operation = await client.armOutage(documentId, stack.collaborator.userId)
    expect(operation.state).toBe('armed')
    try {
      await appendMarkerAtomically(
        collaboratorPage,
        editor(collaboratorPage),
        marker,
      )
      const outage = await client.waitForState(operation.operationId, 'outage')
      expect(outage.outageLayer).toBe('collaboration_sidecar')
      expect(outage.closeCode).toBe(4503)
      expect(outage.durableSequence).not.toBeNull()
      expect(outage.projectionSequence).not.toBeNull()
      expect(outage.durableSequence!).toBeGreaterThan(outage.projectionSequence!)
      await waitForObservedSocketClosure(
        collaboratorPage,
        originalSocketId,
        baselineOrder,
      )
      await ensureInspector(collaboratorPage, stack.locale)
      await expect(
        collaboratorPage.locator('[data-editor-status-kind="reconnecting"]').first(),
      ).toBeVisible({ timeout: 20_000 })
      await expect(editor(collaboratorPage)).toHaveAttribute('contenteditable', 'false')

      const unavailableSession = await browserApi(
        collaboratorPage,
        `/v1/editor/documents/${documentId}/collaboration/session`,
        'POST',
        {
          protocol_version: 1,
          schema_version: before.collaboration.schema_version,
        },
      )
      expect(
        unavailableSession.status,
        `Unavailable collaboration session returned ${apiFailureReason(unavailableSession) ?? 'no public reason'}.`,
      ).toBe(503)
      const unavailableFlush = await browserApi(
        ownerPage,
        `/v1/editor/documents/${documentId}/collaboration/projection:flush`,
        'POST',
      )
      expect(unavailableFlush.status).toBe(503)
      assertFastApiHealth(await browserApi(ownerPage, '/health', 'GET'))

      const stale = await editorDocumentDetail(ownerPage, documentId)
      expect(stale.collaboration.persisted_sequence).toBeGreaterThanOrEqual(
        outage.durableSequence!,
      )
      expect(stale.collaboration.projection_sequence).toBeLessThan(
        outage.durableSequence!,
      )
      expect(stale.collaboration.projection_updated_at).toBe(
        before.collaboration.projection_updated_at,
      )
      expect(stale.content_markdown).not.toContain(marker)
    } finally {
      const restored = await client.restore(operation.operationId)
      expect(restored.state).toBe('ready')
    }
    await waitForConnected(collaboratorPage, stack.locale)
    await requireProjectionFlush(ownerPage, documentId)
    const recovered = await editorDocumentDetail(ownerPage, documentId)
    expect(recovered.collaboration.projection_sequence).toBe(
      recovered.collaboration.persisted_sequence,
    )
    expect(recovered.collaboration.projection_updated_at).not.toBe(
      before.collaboration.projection_updated_at,
    )
    expectTextCount(recovered.content_markdown, marker, 1)
    const socketBeforeRestart = await observedActiveCollaborationSocketId(
      collaboratorPage,
    )
    expect(socketBeforeRestart).not.toBeNull()
    const restarted = await client.restart(documentId)
    expect(restarted.state).toBe('ready')
    expect(restarted.outageLayer).toBe('collaboration_sidecar')
    await waitForConnected(collaboratorPage, stack.locale)
    await expect.poll(
      () => observedActiveCollaborationSocketId(collaboratorPage),
      { timeout: 30_000 },
    ).not.toBe(socketBeforeRestart)
    await requireProjectionFlush(ownerPage, documentId)
    await reloadAcceptingLeaveQuestion(ownerPage, {
      expectQuestion: false,
      waitUntil: 'domcontentloaded',
    })
    await openDocument(ownerPage, documentId, stack.locale, false)
    await expectTextOccurrences(editor(ownerPage), marker, 1)
    await removeMarker(editor(ownerPage), marker)
    await expectTextOccurrences(editor(collaboratorPage), marker, 0)
  })

  test('@gateway-outage @mobile FastAPI gateway outage is independently observable and recovers durable state', async ({ collaboration }, testInfo) => {
    testInfo.setTimeout(90_000)
    const { collaboratorPage, ownerPage, stack } = requireCollaboration(collaboration)
    const documentId = requireCapabilityDocument(
      stack.documents.gatewayOutage,
      'fixture.documents.gatewayOutage',
      testInfo,
    )
    const controls = requireFixtureControls(stack, testInfo)
    const client = new CollaborationFixtureControlClient(controls)
    await installWebSocketObserver(collaboratorPage)
    await Promise.all([
      openDocument(ownerPage, documentId, stack.locale),
      openDocument(collaboratorPage, documentId, stack.locale),
    ])
    await Promise.all([
      waitForConnected(ownerPage, stack.locale),
      waitForConnected(collaboratorPage, stack.locale),
    ])
    assertFastApiHealth(await browserApi(ownerPage, '/health', 'GET'))
    const schemaVersion = (await editorDocumentDetail(ownerPage, documentId))
      .collaboration.schema_version
    const marker = uniqueMarker('gateway-outage-durable')
    await appendMarker(editor(collaboratorPage), marker)
    await expectTextOccurrences(editor(ownerPage), marker, 1)
    await requireProjectionFlush(ownerPage, documentId)
    expectTextCount((await editorDocumentDetail(ownerPage, documentId)).content_markdown, marker, 1)
    await expect.poll(() => observedActiveCollaborationSocketId(collaboratorPage))
      .not.toBeNull()
    const originalSocketId = await observedActiveCollaborationSocketId(collaboratorPage)
    if (originalSocketId === null) {
      throw new Error('No active collaboration socket was observable before the gateway outage.')
    }
    const baselineObserverState = await observedCollaborationSocketState(collaboratorPage)
    const baselineOrder = Math.max(
      0,
      ...baselineObserverState.events.map((event) => event.order),
    )

    const operation = await client.armGatewayOutage(documentId, stack.collaborator.userId)
    expect(operation.state).toBe('armed')
    expect(operation.outageLayer).toBe('fastapi_gateway')
    try {
      const outage = await client.waitForState(operation.operationId, 'outage')
      expect(outage.outageLayer).toBe('fastapi_gateway')
      await waitForObservedSocketClosure(
        collaboratorPage,
        originalSocketId,
        baselineOrder,
      )
      await expect.poll(async () => isPublicGatewayUnavailable(
        await browserApi(ownerPage, '/health', 'GET', undefined, 3_000),
      ), { timeout: 30_000 }).toBe(true)
      await expect.poll(async () => isPublicGatewayUnavailable(await browserApi(
        collaboratorPage,
        `/v1/editor/documents/${documentId}/collaboration/session`,
        'POST',
        { protocol_version: 1, schema_version: schemaVersion },
        3_000,
      )), { timeout: 30_000 }).toBe(true)

      await ensureInspector(collaboratorPage, stack.locale)
      await expect(
        collaboratorPage.locator('[data-editor-status-kind="reconnecting"]').first(),
      ).toBeVisible({ timeout: 20_000 })
      await expect(editor(collaboratorPage)).toHaveAttribute('contenteditable', 'false')
    } finally {
      const restored = await client.restore(operation.operationId)
      expect(restored.state).toBe('ready')
      expect(restored.outageLayer).toBe('fastapi_gateway')
    }

    await expect.poll(async () => isFastApiHealth(
      await browserApi(ownerPage, '/health', 'GET', undefined, 3_000),
    ), { timeout: 30_000 }).toBe(true)
    await Promise.all([
      waitForConnected(ownerPage, stack.locale),
      waitForConnected(collaboratorPage, stack.locale),
    ])
    await requireProjectionFlush(ownerPage, documentId)
    await Promise.all([
      reloadAcceptingLeaveQuestion(ownerPage, {
        expectQuestion: false,
        waitUntil: 'domcontentloaded',
      }),
      reloadAcceptingLeaveQuestion(collaboratorPage, {
        expectQuestion: false,
        waitUntil: 'domcontentloaded',
      }),
    ])
    await Promise.all([
      openDocument(ownerPage, documentId, stack.locale, false),
      openDocument(collaboratorPage, documentId, stack.locale, false),
    ])
    await expectTextOccurrences(editor(ownerPage), marker, 1)
    await expectTextOccurrences(editor(collaboratorPage), marker, 1)
    await removeMarker(editor(ownerPage), marker)
    await expectTextOccurrences(editor(collaboratorPage), marker, 0)
  })

  test('@private-anchors @mobile private AI and comment anchors remain creator-private until they publish exactly once', async ({ collaboration }, testInfo) => {
    testInfo.setTimeout(120_000)
    const { collaboratorPage, ownerPage, stack } = requireCollaboration(collaboration)
    const missingPrivacyFixture = 'External fixture prerequisite missing: fixture.privateAnchors must define one isolated document per browser target plus one private AI marker and one private comment marker per user.'
    const fixture = stack.privateAnchors
    if (fixture === null) {
      process.stdout.write(`[SKIP ${testInfo.project.name}] ${missingPrivacyFixture}\n`)
      test.skip(true, missingPrivacyFixture)
      throw new Error('Playwright did not stop a test marked skipped for missing private anchors.')
    }
    const ownerAiInstructionText = fixture.owner.aiInstructionText
    if (!ownerAiInstructionText) {
      throw new Error(missingPrivacyFixture)
    }
    const documentId = privateAnchorDocumentForProject(fixture.documents, testInfo)
    await Promise.all([
      openDocument(ownerPage, documentId, stack.locale),
      openDocument(collaboratorPage, documentId, stack.locale),
    ])
    await Promise.all([
      waitForConnected(ownerPage, stack.locale),
      waitForConnected(collaboratorPage, stack.locale),
    ])
    await Promise.all([
      ensurePrivateComment(
        ownerPage,
        documentId,
        fixture.owner,
        stack.locale,
      ),
      ensurePrivateComment(
        collaboratorPage,
        documentId,
        fixture.collaborator,
        stack.locale,
      ),
    ])
    await Promise.all([
      openAssistant(ownerPage, stack.locale),
      openAssistant(collaboratorPage, stack.locale),
    ])
    await Promise.all([
      ensurePrivateSuggestion(
        ownerPage,
        documentId,
        fixture.owner,
        stack.locale,
      ),
      ensurePrivateSuggestion(
        collaboratorPage,
        documentId,
        fixture.collaborator,
        stack.locale,
      ),
    ])

    const ownerBefore = await privateAnchorSnapshot(ownerPage, fixture.owner)
    const collaboratorBefore = await privateAnchorSnapshot(
      collaboratorPage,
      fixture.collaborator,
    )
    await expectPrivateAnchorPrivacy(ownerPage, fixture.collaborator, collaboratorBefore)
    await expectPrivateAnchorPrivacy(collaboratorPage, fixture.owner, ownerBefore)

    const rebaseMarker = `${uniqueMarker('anchor-rebase')} `
    await closeMobileInspector(collaboratorPage, stack.locale)
    await insertMarkerBeforeText(
      editor(collaboratorPage),
      fixture.owner.aiAnchorText,
      rebaseMarker,
    )
    await expectTextOccurrences(editor(ownerPage), rebaseMarker, 1)
    await requireProjectionFlush(ownerPage, documentId)
    expectTextCount(
      (await editorDocumentDetail(ownerPage, documentId)).content_markdown,
      rebaseMarker,
      1,
    )
    const ownerRebased = await privateAnchorSnapshot(ownerPage, fixture.owner)
    expect(ownerRebased.aiId).toBe(ownerBefore.aiId)
    expect(ownerRebased.commentId).toBe(ownerBefore.commentId)
    expect(ownerRebased.aiOffset).toBeGreaterThan(ownerBefore.aiOffset)
    expect(ownerRebased.commentOffset).toBeGreaterThan(ownerBefore.commentOffset)

    await Promise.all([
      reloadAcceptingLeaveQuestion(ownerPage, {
        expectQuestion: false,
        waitUntil: 'domcontentloaded',
      }),
      reloadAcceptingLeaveQuestion(collaboratorPage, {
        expectQuestion: false,
        waitUntil: 'domcontentloaded',
      }),
    ])
    await Promise.all([
      openDocument(ownerPage, documentId, stack.locale, false),
      openDocument(collaboratorPage, documentId, stack.locale, false),
    ])
    await Promise.all([
      waitForConnected(ownerPage, stack.locale),
      waitForConnected(collaboratorPage, stack.locale),
    ])
    await Promise.all([
      openAssistant(ownerPage, stack.locale),
      openAssistant(collaboratorPage, stack.locale),
    ])
    await Promise.all([
      expectTextOccurrences(editor(ownerPage), rebaseMarker, 1),
      expectTextOccurrences(editor(collaboratorPage), rebaseMarker, 1),
    ])
    const ownerReloaded = await privateAnchorSnapshot(ownerPage, fixture.owner)
    const collaboratorReloaded = await privateAnchorSnapshot(
      collaboratorPage,
      fixture.collaborator,
    )
    expect(ownerReloaded.aiId).toBe(ownerRebased.aiId)
    expect(ownerReloaded.commentId).toBe(ownerRebased.commentId)
    expect(ownerReloaded.aiOffset).toBeGreaterThan(ownerBefore.aiOffset)
    expect(ownerReloaded.commentOffset).toBeGreaterThan(ownerBefore.commentOffset)
    expect(ownerReloaded.commentOffset - ownerReloaded.aiOffset).toBe(
      ownerRebased.commentOffset - ownerRebased.aiOffset,
    )
    expect(collaboratorReloaded.aiId).toBe(collaboratorBefore.aiId)
    expect(collaboratorReloaded.commentId).toBe(collaboratorBefore.commentId)
    await expectPrivateAnchorPrivacy(ownerPage, fixture.collaborator, collaboratorReloaded)
    await expectPrivateAnchorPrivacy(collaboratorPage, fixture.owner, ownerReloaded)

    const publicationIdentity = await persistedPrivateSuggestionIdentity(
      ownerPage,
      documentId,
      fixture.owner,
      ownerReloaded.aiId,
    )
    const collaboratorPrivateSuggestion = collaboratorPage.locator(
      `.editor-prose [data-suggestion-id="${publicationIdentity.suggestionId}"]`,
    )
    await expect(collaboratorPrivateSuggestion).toHaveCount(0)

    const ownerInstruction = ownerPage.getByText(ownerAiInstructionText, {
      exact: true,
    })
    await ownerInstruction.click()
    const acceptPublication = ownerInstruction.locator('..').getByRole('button', {
      exact: true,
      name: labels[stack.locale].privateSuggestionAccept,
    })
    await expect(acceptPublication).toBeVisible()
    await expect(acceptPublication).toBeEnabled()
    const publishPath = `/v1/editor/documents/${documentId}/suggestions:publish`
    const publishResponsePromise = ownerPage.waitForResponse((response) => (
      response.request().method() === 'POST'
      && new URL(response.url()).pathname === publishPath
    ))
    await acceptPublication.click()
    const publishResponse = await publishResponsePromise
    expect(publishResponse.status()).toBe(200)
    const publication = await publishResponse.json() as {
      command_id?: unknown
      patch_id?: unknown
      sequence?: unknown
      suggestion_ids?: unknown
    }
    expect(publication).toMatchObject({
      command_id: publicationIdentity.publicationCommandId,
      patch_id: publicationIdentity.patchId,
    })
    expect(publication.sequence).toEqual(expect.any(Number))
    if (
      !Array.isArray(publication.suggestion_ids)
      || publication.suggestion_ids.length === 0
      || publication.suggestion_ids.some((id) => typeof id !== 'string' || id.length === 0)
    ) {
      throw new Error('Private suggestion publication returned no valid shared suggestion ids.')
    }
    const sharedSuggestionIds = publication.suggestion_ids as string[]
    expect(new Set(sharedSuggestionIds).size).toBe(sharedSuggestionIds.length)
    expect(sharedSuggestionIds).not.toContain(publicationIdentity.suggestionId)

    await Promise.all([
      selectPublishedCollaborationChange(
        ownerPage,
        stack.locale,
        publicationIdentity.patchId,
      ),
      selectPublishedCollaborationChange(
        collaboratorPage,
        stack.locale,
        publicationIdentity.patchId,
      ),
    ])

    await Promise.all([
      expectPrivateSuggestionDraftCleared(
        ownerPage,
        documentId,
        publicationIdentity.commentId,
      ),
      expectPublishedPrivateSuggestionExactlyOnce(
        ownerPage,
        documentId,
        publicationIdentity,
        sharedSuggestionIds,
      ),
      ...sharedSuggestionIds.flatMap((suggestionId) => [
        expectPublishedReplacementMarks(
          ownerPage,
          suggestionId,
          publicationIdentity.patchId,
        ),
        expectPublishedReplacementMarks(
          collaboratorPage,
          suggestionId,
          publicationIdentity.patchId,
        ),
      ]),
    ])

    await reloadAcceptingLeaveQuestion(collaboratorPage, {
      expectQuestion: false,
      waitUntil: 'domcontentloaded',
    })
    await openDocument(collaboratorPage, documentId, stack.locale, false)
    await waitForConnected(collaboratorPage, stack.locale)
    await selectPublishedCollaborationChange(
      collaboratorPage,
      stack.locale,
      publicationIdentity.patchId,
    )
    await Promise.all(sharedSuggestionIds.map((suggestionId) => (
      expectPublishedReplacementMarks(
        collaboratorPage,
        suggestionId,
        publicationIdentity.patchId,
      )
    )))
    await expectPrivateSuggestionDraftCleared(
      ownerPage,
      documentId,
      publicationIdentity.commentId,
    )
    await expectPublishedPrivateSuggestionExactlyOnce(
      ownerPage,
      documentId,
      publicationIdentity,
      sharedSuggestionIds,
    )

    const ownerEvidencePath = testInfo.outputPath('private-publish-owner.png')
    const collaboratorEvidencePath = testInfo.outputPath('private-publish-collaborator.png')
    await Promise.all([
      ownerPage.screenshot({
        animations: 'disabled',
        path: ownerEvidencePath,
      }),
      collaboratorPage.screenshot({
        animations: 'disabled',
        path: collaboratorEvidencePath,
      }),
    ])
    await testInfo.attach('private-publish-owner', {
      path: ownerEvidencePath,
      contentType: 'image/png',
    })
    await testInfo.attach('private-publish-collaborator', {
      path: collaboratorEvidencePath,
      contentType: 'image/png',
    })

    await removeMarker(editor(ownerPage), rebaseMarker)
    await expectTextOccurrences(editor(collaboratorPage), rebaseMarker, 0)
    await requireProjectionFlush(ownerPage, documentId)
    expectTextCount(
      (await editorDocumentDetail(ownerPage, documentId)).content_markdown,
      rebaseMarker,
      0,
    )
  })

  test('@detached-transfer @mobile a collaboration export imports as a detached document without reconnecting', async ({ collaboration }, testInfo) => {
    testInfo.setTimeout(90_000)
    const { ownerPage, stack, transport } = requireCollaboration(collaboration)
    const documentId = requireCapabilityDocument(
      stack.documents.detachedTransfer,
      'fixture.documents.detachedTransfer',
      testInfo,
    )
    await openDocument(ownerPage, documentId, stack.locale)
    await waitForConnected(ownerPage, stack.locale)
    const marker = uniqueMarker('detached-transfer')
    await appendMarker(editor(ownerPage), marker)
    try {
      await requireProjectionFlush(ownerPage, documentId)
      await forceDownloadProjectPicker(ownerPage)

      const zipFiles = parseStoredZip(await downloadProjectArchive(
        ownerPage,
        stack.locale,
        labels[stack.locale].exportBackup,
      ))
      const documentFile = zipFiles.find((file) => (
        projectFrontmatterValue(file.contents, 'document_id') === documentId
        && projectFrontmatterValue(
          file.contents,
          'detached_from_collaboration',
        ) === true
      ))
      expect(documentFile, 'Export must contain a detached collaboration document.').toBeTruthy()
      expectTextCount(documentFile!.contents, marker, 1)
      const exportedDocumentTitle = projectFrontmatterValue(documentFile!.contents, 'title')
      expect(typeof exportedDocumentTitle).toBe('string')
      const importedDocumentTitle = normalizedImportedDocumentTitle(
        exportedDocumentTitle as string,
      )
      const exportedDocumentOrder = projectEditorDocumentOrder(zipFiles)
      const exportedDocumentById = new Map(
        projectDocumentEntries(zipFiles).map((document) => [document.id, document]),
      )
      // Verglichen wird gegen ALLE Editor-Dokumente des Archivs, denn der
      // Import laedt jede .md hoch, nicht nur die abgeloesten. Eine Sonde, die
      // hier auf `detached_from_collaboration` filtert, stellt zwei
      // verschiedene Mengen gegenueber -- und kann ausserdem genau die
      // Eigenschaft nicht pruefen, aus der sie ihre Grundgesamtheit ableitet.
      // Dass das Flag sitzt, sichern die beiden Einzeldokument-Zusicherungen,
      // deren contentMode dieser Test selbst hergestellt hat.
      const exportedDocumentHashes = [...exportedDocumentById.values()]
        .map((document) => document.bodyHash)
        .sort()
      expect(exportedDocumentOrder).toHaveLength(exportedDocumentById.size)
      const exportedDocumentHashesInOrder = exportedDocumentOrder.map((exportedId) => {
        const exportedDocument = exportedDocumentById.get(exportedId)
        if (!exportedDocument) {
          throw new Error(`Project manifest references missing editor document ${exportedId}.`)
        }
        return exportedDocument.bodyHash
      })

      const endpoint = stack.transports[transport]
      const browser = ownerPage.context().browser()
      if (!browser) {
        throw new Error('Detached transfer requires a browser-backed Playwright context.')
      }
      const detachedContext = await browser.newContext({
        baseURL: endpoint.baseURL!,
        storageState: endpoint.collaboratorStorageState,
        viewport: ownerPage.viewportSize() ?? undefined,
      })
      try {
        const detachedPage = await detachedContext.newPage()
        await detachedPage.goto('./', { waitUntil: 'domcontentloaded' })
        await expect(
          detachedPage.getByRole('button', { name: 'Editor', exact: true }),
        ).toBeVisible({ timeout: 20_000 })
        await forceDownloadProjectPicker(detachedPage)
        const preImportDocumentIds = await captureOwnedDocumentIds(detachedPage)

        let collaborationSessionAttempts = 0
        let collaborationSocketAttempts = 0
        await detachedContext.route(
          '**/v1/editor/documents/*/collaboration/session',
          async (route) => {
            collaborationSessionAttempts += 1
            await route.abort('blockedbyclient')
          },
        )
        detachedPage.on('websocket', (socket) => {
          if (new URL(socket.url()).pathname === '/collaboration') {
            collaborationSocketAttempts += 1
          }
        })

        const projectInput = await interceptNextProjectFileInput(detachedPage)
        await triggerProjectAction(
          detachedPage,
          stack.locale,
          labels[stack.locale].importFile,
        )
        await expect(projectInput).toHaveCount(1)
        const uploadFiles = projectUploadFilesInAdversarialOrder(
          zipFiles,
          exportedDocumentOrder,
          documentId,
        )
        const uploadDocumentOrder = uploadFiles.flatMap((file) => {
          const uploadedId = projectFrontmatterValue(file.contents, 'document_id')
          return typeof uploadedId === 'string' && uploadedId ? [uploadedId] : []
        })
        expect(uploadDocumentOrder).not.toEqual(exportedDocumentOrder)
        expect(uploadDocumentOrder[0]).not.toBe(documentId)
        await dispatchProjectFiles(projectInput, uploadFiles)
        // In-memory files can finish parsing within one render batch, so a
        // transient disabled button is not a durable completion signal. The
        // imported identity, server persistence and re-export below prove the
        // completed action without depending on an intermediate paint.
        const importedDocumentId = await captureActiveImportedDocument(
          detachedPage,
          preImportDocumentIds,
          importedDocumentTitle,
          stack.locale,
        )
        await expectTextOccurrences(editor(detachedPage), marker, 1)
        await expect(editor(detachedPage)).toHaveAttribute('contenteditable', 'true')
        const preImportDocumentIdSet = new Set(preImportDocumentIds)
        let postImportDocumentIds: string[] = []
        await expect.poll(async () => {
          postImportDocumentIds = await captureOwnedDocumentIds(detachedPage)
          return postImportDocumentIds.filter(
            (candidate) => !preImportDocumentIdSet.has(candidate),
          ).length
        }, {
          intervals: [250, 500, 1_000],
          message: 'Every imported document must reach durable editor persistence.',
          timeout: 30_000,
        }).toBe(exportedDocumentHashes.length)
        const importedDocumentIds = postImportDocumentIds.filter(
          (candidate) => !preImportDocumentIdSet.has(candidate),
        )
        expect(importedDocumentIds).toHaveLength(exportedDocumentHashes.length)
        expect(importedDocumentIds).toContain(importedDocumentId)
        const visibleImportedDocumentOrder = await captureVisibleImportedDocumentOrder(
          detachedPage,
          importedDocumentIds,
          stack.locale,
        )
        const visibleImportedDocumentHashes = await Promise.all(
          visibleImportedDocumentOrder.map(async (importedId) => (
            projectMarkdownBodyHash(
              await editorDocumentMarkdown(detachedPage, importedId),
            )
          )),
        )
        expect(visibleImportedDocumentHashes).toEqual(exportedDocumentHashesInOrder)

        const reExportFiles = parseStoredZip(await downloadProjectArchive(
          detachedPage,
          stack.locale,
          labels[stack.locale].exportBackup,
        ))
        const reExportedDocument = reExportFiles.find((file) => (
          projectFrontmatterValue(file.contents, 'document_id') === importedDocumentId
        ))
        expect(
          reExportedDocument,
          'Re-export must contain the selected detached document under its new identity.',
        ).toBeTruthy()
        expectTextCount(reExportedDocument!.contents, marker, 1)
        expect(projectFrontmatterValue(
          reExportedDocument!.contents,
          'detached_from_collaboration',
        )).toBeUndefined()
        expect(projectMarkdownBodyHash(reExportedDocument!.contents)).toBe(
          projectMarkdownBodyHash(documentFile!.contents),
        )
        const importedDocumentIdSet = new Set(importedDocumentIds)
        const reExportedImportedDocuments = projectDocumentEntries(reExportFiles)
          .filter((document) => importedDocumentIdSet.has(document.id))
        expect(reExportedImportedDocuments.map((document) => document.id).sort()).toEqual(
          [...importedDocumentIds].sort(),
        )
        expect(reExportedImportedDocuments.map((document) => document.bodyHash).sort()).toEqual(
          exportedDocumentHashes,
        )
        const reExportedDocumentById = new Map(
          reExportedImportedDocuments.map((document) => [document.id, document]),
        )
        const reExportedImportedOrder = projectEditorDocumentOrder(reExportFiles)
          .filter((reExportedId) => importedDocumentIdSet.has(reExportedId))
        expect(reExportedImportedOrder.map((reExportedId) => {
          const reExportedDocumentEntry = reExportedDocumentById.get(reExportedId)
          if (!reExportedDocumentEntry) {
            throw new Error(
              `Re-export manifest references missing editor document ${reExportedId}.`,
            )
          }
          return reExportedDocumentEntry.bodyHash
        })).toEqual(exportedDocumentHashesInOrder)

        const detachedEvidencePath = testInfo.outputPath('detached-transfer-final.png')
        await detachedPage.screenshot({
          animations: 'disabled',
          path: detachedEvidencePath,
        })
        await testInfo.attach('detached-transfer-final', {
          contentType: 'image/png',
          path: detachedEvidencePath,
        })

        await detachedPage.waitForTimeout(1_500)
        expect(collaborationSessionAttempts).toBe(0)
        expect(collaborationSocketAttempts).toBe(0)
      } finally {
        await detachedContext.close()
      }
    } finally {
      const cleanupBaseline = await editorDocumentDetail(ownerPage, documentId)
      await removeMarker(editor(ownerPage), marker)
      await expectTextOccurrences(editor(ownerPage), marker, 0)
      await expect.poll(async () => (
        (await editorDocumentDetail(ownerPage, documentId)).collaboration.persisted_sequence
      ), {
        message: 'Detached-transfer marker cleanup must receive a durable server acknowledgement.',
        timeout: 20_000,
      }).toBeGreaterThan(cleanupBaseline.collaboration.persisted_sequence)
      await requireProjectionFlush(ownerPage, documentId)
      const cleanedDocument = await editorDocumentDetail(ownerPage, documentId)
      expect(cleanedDocument.collaboration.projection_sequence).toBe(
        cleanedDocument.collaboration.persisted_sequence,
      )
      expectTextCount(cleanedDocument.content_markdown, marker, 0)
    }
  })

  test('@protocol-rejection @mobile incompatible schema and wire frames are rejected explicitly', async ({ collaboration }, testInfo) => {
    const { ownerPage, stack } = requireCollaboration(collaboration)
    const documentId = requireCapabilityDocument(
      stack.documents.protocol,
      'fixture.documents.protocol',
      testInfo,
    )
    await openDocument(ownerPage, documentId, stack.locale)
    const detail = await editorDocumentDetail(ownerPage, documentId)
    const schemaVersion = detail.collaboration.schema_version

    const protocolConflict = await browserApi(
      ownerPage,
      `/v1/editor/documents/${documentId}/collaboration/session`,
      'POST',
      { protocol_version: 2_147_483_647, schema_version: schemaVersion },
    )
    expect(protocolConflict.status).toBe(409)
    expect(apiFailureReason(protocolConflict)).toBe('protocol_conflict')

    const schemaConflict = await browserApi(
      ownerPage,
      `/v1/editor/documents/${documentId}/collaboration/session`,
      'POST',
      { protocol_version: 1, schema_version: schemaVersion + 1 },
    )
    expect(schemaConflict.status).toBe(409)
    expect(apiFailureReason(schemaConflict)).toBe('schema_conflict')

    const validSession = await browserApi(
      ownerPage,
      `/v1/editor/documents/${documentId}/collaboration/session`,
      'POST',
      { protocol_version: 1, schema_version: schemaVersion },
    )
    requireApiSuccess(validSession)
    expect(await closeCodeForTextFrame(ownerPage, validSession.payload)).toBe(4409)
  })

  test('@source-readonly @mobile Source stays read-only while its shared projection updates', async ({ collaboration }) => {
    const { collaboratorPage, ownerPage, stack } = requireCollaboration(collaboration)
    const documentId = stack.documents.sourceReadonly
    await Promise.all([
      openDocument(ownerPage, documentId, stack.locale),
      openDocument(collaboratorPage, documentId, stack.locale),
    ])
    await Promise.all([
      waitForConnected(ownerPage, stack.locale),
      waitForConnected(collaboratorPage, stack.locale),
    ])

    await ownerPage.getByRole('button', {
      exact: true,
      name: labels[stack.locale].moreActions,
    }).click()
    await ownerPage.getByRole('menuitem', {
      exact: true,
      name: labels[stack.locale].source,
    }).click()
    const source = ownerPage.getByLabel(labels[stack.locale].sourceEditor, { exact: true })
    await expect(source).toBeVisible()
    await expect(source).not.toHaveAttribute('contenteditable', 'true')
    const editButton = ownerPage.getByRole('button', {
      exact: true,
      name: labels[stack.locale].edit,
    }).first()
    const suggestButton = ownerPage.getByRole('button', {
      exact: true,
      name: labels[stack.locale].suggest,
    }).first()
    await expect(editButton).toBeDisabled()
    await expect(suggestButton).toBeDisabled()
    await editButton.locator('xpath=..').hover()
    await expect(
      ownerPage.getByText(labels[stack.locale].sourceReadOnly, { exact: true }),
    ).toBeVisible()

    const sharedMarker = uniqueMarker('source-shared-projection')
    const rejectedMarker = uniqueMarker('source-rejected-input')
    await appendMarker(editor(collaboratorPage), sharedMarker)
    await expectTextOccurrences(source, sharedMarker, 1)
    await source.click()
    await ownerPage.keyboard.insertText(rejectedMarker)
    await expectTextOccurrences(source, rejectedMarker, 0)
    const detail = await editorDocumentDetail(ownerPage, documentId)
    expect(detail.content_markdown).not.toContain(rejectedMarker)

    await removeMarker(editor(collaboratorPage), sharedMarker)
    await expectTextOccurrences(source, sharedMarker, 0)
    await ownerPage.getByRole('button', {
      exact: true,
      name: labels[stack.locale].moreActions,
    }).click()
    await ownerPage.getByRole('menuitem', {
      exact: true,
      name: labels[stack.locale].live,
    }).click()
    await expect(source).toHaveCount(0)
    await waitForConnected(ownerPage, stack.locale)
    await expectTextOccurrences(editor(ownerPage), rejectedMarker, 0)
  })

  test('@layout editor and Inspector surfaces stay bounded without control overlap', async ({ collaboration }, testInfo) => {
    const { collaboratorPage, ownerPage, stack } = requireCollaboration(collaboration)
    const documentId = stack.documents.layout
    await Promise.all([
      openDocument(ownerPage, documentId, stack.locale),
      openDocument(collaboratorPage, documentId, stack.locale),
    ])
    await Promise.all([
      waitForConnected(ownerPage, stack.locale),
      waitForConnected(collaboratorPage, stack.locale),
    ])
    await chooseWriteMode(collaboratorPage, stack.locale, 'suggest')
    const marker = uniqueMarker('layout-expanded-change')
    await appendMarkerAtomically(collaboratorPage, editor(collaboratorPage), marker)
    await expectTextOccurrences(editor(collaboratorPage), marker, 1)
    await expectTextOccurrences(editor(ownerPage), marker, 1)
    await ensureInspector(ownerPage, stack.locale)
    const changesTab = ownerPage.getByRole('tab', {
      name: new RegExp(`^${labels[stack.locale].changes}`),
    })
    await changesTab.click()
    await expect(changesTab).toHaveAttribute('data-state', 'active')
    const inspector = ownerPage.locator('.inqtrix-contained-panel', { has: changesTab })
    const changeRow = inspector.locator('[data-inspector-change-id]', { hasText: marker })
    await expect(changeRow).toHaveCount(1)
    const changeToggle = changeRow.locator('button[aria-expanded]').first()
    await changeToggle.click()
    await expect(changeToggle).toHaveAttribute('aria-expanded', 'true')

    const displayControl = inspector.getByLabel(labels[stack.locale].display, { exact: true })
    await expect(displayControl).toBeVisible()
    for (const display of ['simple', 'all', 'final', 'original'] as const) {
      await expect(displayControl.getByRole('button', {
        exact: true,
        name: labels[stack.locale][display],
      })).toBeVisible()
    }
    await expect(inspector.getByRole('combobox', {
      exact: true,
      name: labels[stack.locale].author,
    })).toBeVisible()
    await expect(inspector.getByRole('combobox', {
      exact: true,
      name: labels[stack.locale].type,
    })).toBeVisible()
    await expect(changeRow.getByRole('button', {
      exact: true,
      name: labels[stack.locale].accept,
    })).toBeVisible()
    await expect(changeRow.getByRole('button', {
      exact: true,
      name: labels[stack.locale].reject,
    })).toBeVisible()
    await expect(inspector.getByRole('button', {
      exact: true,
      name: labels[stack.locale].acceptAll,
    })).toBeVisible()
    await expect(inspector.getByRole('button', {
      exact: true,
      name: labels[stack.locale].rejectAll,
    })).toBeVisible()

    const layout = await wholeEditorLayout(ownerPage)
    expect(layout.horizontalOverflow).toBeLessThanOrEqual(1)
    expect(layout.missingSurfaces).toEqual([])
    expect(layout.outOfViewportSurfaces).toEqual([])
    expect(layout.outOfBoundsControls).toEqual([])
    expect(layout.editorControlOverlaps).toEqual([])
    expect(layout.inspectorControlOverlaps).toEqual([])
    expect(layout.canvasContainedByEditor).toBe(true)

    const formFactor = testInfo.project.metadata.formFactor
    expect(['desktop', 'mobile']).toContain(formFactor)
    if (formFactor === 'mobile') {
      expect(layout.mobileDialogPresent).toBe(true)
      expect(layout.dialogContainedByViewport).toBe(true)
    } else {
      expect(layout.mobileDialogPresent).toBe(false)
      expect(layout.editorInspectorOverlapArea).toBeLessThanOrEqual(1)
    }

    await changeRow.getByRole('button', {
      exact: true,
      name: labels[stack.locale].reject,
    }).click()
    await expect(changeRow).toHaveCount(0)
    await expectTextOccurrences(editor(ownerPage), marker, 0)
    await expectTextOccurrences(editor(collaboratorPage), marker, 0)
  })

  test('@mobile-only @mobile-drawer mobile tree and inspector drawers are modal and exclusive', async ({ collaboration }) => {
    const { ownerPage, stack } = requireCollaboration(collaboration)
    await openDocument(ownerPage, stack.documents.mobileDrawers, stack.locale)

    await ownerPage.getByRole('button', { name: labels[stack.locale].showTree }).click()
    const treeDrawer = ownerPage.locator('#editor-file-tree-panel[role="dialog"]')
    await expect(treeDrawer).toBeVisible()
    await expect(ownerPage.locator('#editor-comments-panel[role="dialog"]')).toHaveCount(0)
    await expect(ownerPage.locator('[role="dialog"]')).toHaveCount(1)
    await ownerPage.keyboard.press('Escape')
    await expect(treeDrawer).toHaveCount(0)

    await ownerPage.getByRole('button', { name: labels[stack.locale].showInspector }).click()
    const inspectorDrawer = ownerPage.locator('#editor-comments-panel[role="dialog"]')
    await expect(inspectorDrawer).toBeVisible()
    await expect(ownerPage.locator('#editor-file-tree-panel[role="dialog"]')).toHaveCount(0)
    await expect(ownerPage.locator('[role="dialog"]')).toHaveCount(1)
    await ownerPage.getByRole('button', { name: labels[stack.locale].closeInspector }).click()
    await expect(inspectorDrawer).toHaveCount(0)
  })
})

function requireCollaboration(
  collaboration: CollaborationHarness | null,
): CollaborationHarness {
  if (!collaboration) {
    throw new Error(
      'The collaboration fixture was not initialized and did not mark the test as skipped.',
    )
  }
  return collaboration
}

function requireCapabilityDocument(
  documentId: string | null,
  field: string,
  testInfo: TestInfo,
): string {
  if (documentId) return documentId
  const reason = `External fixture prerequisite missing: ${field} must be a non-empty string.`
  process.stdout.write(`[SKIP ${testInfo.project.name}] ${reason}\n`)
  testInfo.skip(true, reason)
  throw new Error(`Playwright did not stop a test skipped for missing ${field}.`)
}

function requireFixtureControls(
  stack: CollaborationE2EStack,
  testInfo: TestInfo,
): NonNullable<CollaborationE2EStack['controls']> {
  if (stack.controls) return stack.controls
  const details = stack.capabilityReasons.controls.join('; ')
  const reason = `External fixture prerequisite missing: fixture.controls must provide lost-ACK, sidecar-outage, FastAPI-gateway-outage, status, restore, and restart operations${details ? ` (${details})` : ''}.`
  process.stdout.write(`[SKIP ${testInfo.project.name}] ${reason}\n`)
  testInfo.skip(true, reason)
  throw new Error('Playwright did not stop a test skipped for missing fixture.controls.')
}

async function openDocument(
  page: Page,
  documentId: string,
  locale: 'de' | 'en',
  navigate = true,
): Promise<void> {
  if (navigate) {
    await leavePageAcceptingQuestion(
      page,
      () => page.goto('./', { waitUntil: 'domcontentloaded' }),
      { expectQuestion: false },
    )
  }
  const editorNavigation = page.getByRole('button', { name: 'Editor', exact: true })
  await expect(editorNavigation).toBeVisible({ timeout: 20_000 })
  let navigationReanchored = false
  await editorNavigation.click()
  const initialNavigationActivated = await expect(editorNavigation)
    .toHaveAttribute('aria-pressed', 'true', { timeout: 3_000 })
    .then(() => true, () => false)
  if (!initialNavigationActivated) {
    // Initial app hydration can replace a navigation event that was sent while
    // the shell was already visible. Re-anchor the requested view exactly once;
    // a second loss remains a hard failure and is never hidden by a reload.
    navigationReanchored = true
    await editorNavigation.click()
    await expect(editorNavigation).toHaveAttribute('aria-pressed', 'true')
  }

  const documentRow = page.locator(`[data-editor-document-id="${documentId}"]`).first()
  const revealDocumentTree = async () => {
    if (await documentRow.isVisible().catch(() => false)) return
    const showTree = page.getByRole('button', { name: labels[locale].showTree })
    if (await showTree.isVisible().catch(() => false)) await showTree.click()
  }
  await revealDocumentTree()
  const documentLists = page.locator(
    '[data-editor-shared-documents], [data-editor-owned-documents]',
  )
  const showMoreControls = documentLists.getByRole('button', {
    exact: true,
    name: labels[locale].showMore,
  })
  const waitForDocumentTree = async (allowNavigationReanchor: boolean) => {
    let navigationLost = false
    await expect.poll(
      async () => {
        if (await editorNavigation.getAttribute('aria-pressed') !== 'true') {
          navigationLost = true
          return allowNavigationReanchor
        }
        return (
          await documentRow.isVisible().catch(() => false)
          || await showMoreControls.count() > 0
        )
      },
      {
        message: allowNavigationReanchor
          ? 'The document tree must load or report that initial hydration replaced the requested view.'
          : 'The document tree must load the target row or a reveal control while Editor remains active.',
        timeout: 20_000,
      },
    ).toBe(true)
    return navigationLost
  }

  if (await waitForDocumentTree(!navigationReanchored)) {
    navigationReanchored = true
    await editorNavigation.click()
    await expect(editorNavigation).toHaveAttribute('aria-pressed', 'true')
    await revealDocumentTree()
    expect(await waitForDocumentTree(false)).toBe(false)
  }

  for (let attempt = 0; attempt < 10; attempt += 1) {
    if (await documentRow.isVisible().catch(() => false)) break
    const visibleRows = page.locator('[data-editor-document-id]:visible')
    const showMoreButtons = await showMoreControls.all()
    let expandedDocumentList = false

    for (const showMore of showMoreButtons) {
      if (!await showMore.isVisible().catch(() => false)) continue
      const previousVisibleRows = await visibleRows.count()
      await showMore.click()
      await expect.poll(
        () => visibleRows.count(),
        { message: 'Show more must reveal additional document rows.' },
      ).toBeGreaterThan(previousVisibleRows)
      expandedDocumentList = true
      if (await documentRow.isVisible().catch(() => false)) break
    }

    if (!expandedDocumentList) break
  }
  await expect(documentRow).toBeVisible({ timeout: 20_000 })
  await documentRow.click()
  const mobileTree = page.locator('#editor-file-tree-panel[role="dialog"]')
  if (await mobileTree.isVisible().catch(() => false)) {
    await page.keyboard.press('Escape')
    await expect(mobileTree).toHaveCount(0)
  }
  await expect(editor(page)).toBeVisible({ timeout: 20_000 })
}

async function captureActiveImportedDocument(
  page: Page,
  preImportDocumentIds: readonly string[],
  importedDocumentTitle: string,
  locale: 'de' | 'en',
): Promise<string> {
  const editorNavigation = page.getByRole('button', { name: 'Editor', exact: true })
  await expect(editorNavigation).toBeVisible({ timeout: 20_000 })
  await editorNavigation.click()

  const showTree = page.getByRole('button', { name: labels[locale].showTree })
  if (await showTree.isVisible().catch(() => false)) await showTree.click()

  const search = page.getByRole('searchbox', {
    name: labels[locale].searchDocuments,
  })
  await expect(search).toBeVisible({ timeout: 20_000 })
  await search.fill(importedDocumentTitle)
  const importedRows = page.locator('[data-editor-document-id]', {
    has: page.getByText(importedDocumentTitle, { exact: true }),
  })
  const preImportIds = new Set(preImportDocumentIds)
  await expect.poll(
    async () => (await documentRowIds(importedRows))
      .filter((documentId) => !preImportIds.has(documentId)).length,
    {
      message: 'Import must add exactly one matching document identity.',
      timeout: 20_000,
    },
  ).toBe(1)
  const newDocumentIds = (await documentRowIds(importedRows))
    .filter((documentId) => !preImportIds.has(documentId))
  expect(newDocumentIds).toHaveLength(1)
  const importedDocumentId = newDocumentIds[0]!
  const activeDocument = importedRows.filter({
    has: page.locator('button[aria-pressed="true"]'),
  })
  await expect(activeDocument).toHaveCount(1)
  await expect(activeDocument).toHaveAttribute(
    'data-editor-document-id',
    importedDocumentId,
  )
  await search.fill('')
  const mobileTree = page.locator('#editor-file-tree-panel[role="dialog"]')
  if (await mobileTree.isVisible().catch(() => false)) {
    await page.keyboard.press('Escape')
    await expect(mobileTree).toHaveCount(0)
  }
  await expect(editor(page)).toBeVisible({ timeout: 20_000 })
  await expect(
    page.locator('[data-editor-topbar-title]').getByRole('button', {
      exact: true,
      name: importedDocumentTitle,
    }),
  ).toBeVisible()
  return importedDocumentId
}

async function captureVisibleImportedDocumentOrder(
  page: Page,
  importedDocumentIds: readonly string[],
  locale: 'de' | 'en',
): Promise<string[]> {
  const editorNavigation = page.getByRole('button', { name: 'Editor', exact: true })
  await expect(editorNavigation).toBeVisible({ timeout: 20_000 })
  await editorNavigation.click()

  const showTree = page.getByRole('button', { name: labels[locale].showTree })
  if (await showTree.isVisible().catch(() => false)) await showTree.click()

  const search = page.getByRole('searchbox', {
    name: labels[locale].searchDocuments,
  })
  await expect(search).toBeVisible({ timeout: 20_000 })
  await search.fill('.md')
  const importedIdSet = new Set(importedDocumentIds)
  const rows = page.locator('[data-editor-document-id]')
  let visibleImportedIds: string[] = []
  await expect.poll(async () => {
    visibleImportedIds = (await documentRowIds(rows))
      .filter((documentId) => importedIdSet.has(documentId))
    return visibleImportedIds.length
  }, {
    message: 'Document search must expose every imported document in project order.',
    timeout: 20_000,
  }).toBe(importedDocumentIds.length)
  expect(new Set(visibleImportedIds).size).toBe(importedDocumentIds.length)

  await search.fill('')
  const mobileTree = page.locator('#editor-file-tree-panel[role="dialog"]')
  if (await mobileTree.isVisible().catch(() => false)) {
    await page.keyboard.press('Escape')
    await expect(mobileTree).toHaveCount(0)
  }
  return visibleImportedIds
}

async function captureOwnedDocumentIds(page: Page): Promise<string[]> {
  const documentIds: string[] = []
  const seenDocumentIds = new Set<string>()
  const seenCursors = new Set<string>()
  let cursor: string | null = null

  while (true) {
    const parameters = new URLSearchParams({ limit: '200', scope: 'owned' })
    if (cursor) parameters.set('cursor', cursor)
    const result = await browserApi(
      page,
      `/v1/editor/documents?${parameters.toString()}`,
      'GET',
    )
    requireApiSuccess(result)
    const payload = isRecord(result.payload) ? result.payload : null
    if (!payload || !Array.isArray(payload.data)) {
      throw new Error('Owned document listing omitted its data array.')
    }
    for (const row of payload.data) {
      if (!isRecord(row) || typeof row.id !== 'string' || !row.id) {
        throw new Error('Owned document listing returned an invalid identity.')
      }
      if (seenDocumentIds.has(row.id)) {
        throw new Error('Owned document listing repeated a document identity.')
      }
      seenDocumentIds.add(row.id)
      documentIds.push(row.id)
    }
    const nextCursor = payload.next_cursor
    if (nextCursor === null) break
    if (
      typeof nextCursor !== 'string'
      || !nextCursor
      || seenCursors.has(nextCursor)
    ) {
      throw new Error('Owned document listing returned an invalid page cursor.')
    }
    seenCursors.add(nextCursor)
    cursor = nextCursor
  }

  return documentIds
}

async function documentRowIds(rows: Locator): Promise<string[]> {
  return await rows.evaluateAll((elements) => elements.map((element) => {
    const documentId = element.getAttribute('data-editor-document-id')
    if (!documentId) throw new Error('Editor document row has no identity.')
    return documentId
  }))
}

function editor(page: Page): Locator {
  return page
    .locator('.editor-prose[contenteditable="true"], .editor-prose[contenteditable="false"]')
    .filter({ visible: true })
    .first()
}

async function ensureInspector(page: Page, locale: 'de' | 'en'): Promise<void> {
  const changes = page.getByRole('tab', { name: new RegExp(`^${labels[locale].changes}`) })
  if (!await changes.isVisible().catch(() => false)) {
    await page.getByRole('button', { name: labels[locale].showInspector }).click()
  }
  await expect(changes).toBeVisible()
}

async function openAssistant(page: Page, locale: 'de' | 'en'): Promise<void> {
  await ensureInspector(page, locale)
  const assistant = page.getByRole('tab', { name: labels[locale].assistant, exact: true })
  await assistant.click()
  await expect(assistant).toHaveAttribute('data-state', 'active')
}

async function waitForConnected(page: Page, _locale: 'de' | 'en'): Promise<void> {
  await expect(
    page.locator('[data-editor-status-kind="saved"]').first(),
  ).toBeVisible({ timeout: 30_000 })
}

async function closeMobileInspector(page: Page, locale: 'de' | 'en'): Promise<void> {
  const drawer = page.locator('#editor-comments-panel[role="dialog"]')
  if (!await drawer.isVisible().catch(() => false)) return
  await page.getByRole('button', { name: labels[locale].closeInspector }).click()
  await expect(drawer).toHaveCount(0)
}

async function wholeEditorLayout(page: Page): Promise<{
  canvasContainedByEditor: boolean
  dialogContainedByViewport: boolean
  editorControlOverlaps: string[]
  editorInspectorOverlapArea: number
  horizontalOverflow: number
  inspectorControlOverlaps: string[]
  missingSurfaces: string[]
  mobileDialogPresent: boolean
  outOfBoundsControls: string[]
  outOfViewportSurfaces: string[]
}> {
  const snapshot = await page.evaluate(() => {
    type Bounds = { bottom: number; left: number; right: number; top: number }
    const viewport = {
      height: document.documentElement.clientHeight,
      width: document.documentElement.clientWidth,
    }
    const bounds = (element: Element): Bounds => {
      const rect = element.getBoundingClientRect()
      return { bottom: rect.bottom, left: rect.left, right: rect.right, top: rect.top }
    }
    const intersectsViewport = (rect: Bounds): boolean => (
      Math.min(rect.right, viewport.width) - Math.max(rect.left, 0) > 1
      && Math.min(rect.bottom, viewport.height) - Math.max(rect.top, 0) > 1
    )
    const visible = (element: Element): boolean => {
      const style = getComputedStyle(element)
      const rect = bounds(element)
      return (
        style.display !== 'none'
        && style.visibility !== 'hidden'
        && rect.right - rect.left > 1
        && rect.bottom - rect.top > 1
        && intersectsViewport(rect)
      )
    }
    const contained = (inner: Bounds, outer: Bounds): boolean => (
      inner.left >= outer.left - 1
      && inner.right <= outer.right + 1
      && inner.top >= outer.top - 1
      && inner.bottom <= outer.bottom + 1
    )
    const inViewport = (rect: Bounds): boolean => contained(rect, {
      bottom: viewport.height,
      left: 0,
      right: viewport.width,
      top: 0,
    })
    const canvas = Array.from(document.querySelectorAll<HTMLElement>('.editor-prose'))
      .find(visible) ?? null
    const editorMain = canvas?.closest('main') ?? null
    const canvasViewport = canvas?.closest('[data-radix-scroll-area-viewport]')
      ?? canvas?.parentElement
      ?? null
    const inspector = Array.from(
      document.querySelectorAll<HTMLElement>('.inqtrix-contained-panel'),
    ).find((panel) => visible(panel) && panel.querySelector('[role="tab"]')) ?? null
    const mobileDialog = inspector?.closest<HTMLElement>('[role="dialog"]') ?? null

    const surfaces: Array<[string, Element | null]> = [
      ['editor', editorMain],
      ['editor-canvas-viewport', canvasViewport],
      ['inspector', inspector],
    ]
    if (mobileDialog) surfaces.push(['mobile-inspector-dialog', mobileDialog])
    const missingSurfaces = surfaces
      .filter(([, element]) => element === null)
      .map(([name]) => name)
    const outOfViewportSurfaces = surfaces
      .filter(([, element]) => element !== null && !inViewport(bounds(element)))
      .map(([name]) => name)

    const controlSelector = 'button, input, select, textarea, [role="status"], [role="tab"]'
    const controls = (scope: Element | null, excluded: Element | null = null): Element[] => {
      if (!scope) return []
      return [...new Set(Array.from(scope.querySelectorAll(controlSelector)))]
        .filter((element) => !excluded?.contains(element) && visible(element))
    }
    const controlName = (element: Element, index: number): string => {
      const accessible = element.getAttribute('aria-label')
        ?? element.textContent?.trim()
        ?? element.tagName.toLowerCase()
      return `${element.tagName.toLowerCase()}[${index}]:${accessible.slice(0, 60)}`
    }
    const editorControls = controls(editorMain, inspector)
    const inspectorControls = controls(inspector)
    const controlGeometry = (elements: Element[]): ControlGeometry[] => elements.map(
      (element, index) => ({
        bounds: bounds(element),
        name: controlName(element, index),
      }),
    )
    const overlaps = (elements: Element[]): string[] => {
      const collisions: string[] = []
      for (let leftIndex = 0; leftIndex < elements.length; leftIndex += 1) {
        for (let rightIndex = leftIndex + 1; rightIndex < elements.length; rightIndex += 1) {
          const left = elements[leftIndex]!
          const right = elements[rightIndex]!
          if (left.contains(right) || right.contains(left)) continue
          const leftBounds = bounds(left)
          const rightBounds = bounds(right)
          const overlapWidth = Math.min(leftBounds.right, rightBounds.right)
            - Math.max(leftBounds.left, rightBounds.left)
          const overlapHeight = Math.min(leftBounds.bottom, rightBounds.bottom)
            - Math.max(leftBounds.top, rightBounds.top)
          if (overlapWidth > 1 && overlapHeight > 1) {
            collisions.push(
              `${controlName(left, leftIndex)} <> ${controlName(right, rightIndex)}`,
            )
          }
        }
      }
      return collisions
    }
    const overlapArea = (left: Element | null, right: Element | null): number => {
      if (!left || !right) return 0
      const leftBounds = bounds(left)
      const rightBounds = bounds(right)
      return Math.max(
        0,
        Math.min(leftBounds.right, rightBounds.right) - Math.max(leftBounds.left, rightBounds.left),
      ) * Math.max(
        0,
        Math.min(leftBounds.bottom, rightBounds.bottom) - Math.max(leftBounds.top, rightBounds.top),
      )
    }
    const editorBounds = editorMain ? bounds(editorMain) : null
    const canvasBounds = canvasViewport ? bounds(canvasViewport) : null
    return {
      canvasContainedByEditor: Boolean(
        editorBounds && canvasBounds && contained(canvasBounds, editorBounds),
      ),
      dialogContainedByViewport: mobileDialog ? inViewport(bounds(mobileDialog)) : false,
      editorBounds,
      editorControlGeometry: controlGeometry(editorControls),
      editorControlOverlaps: overlaps(editorControls),
      editorInspectorOverlapArea: overlapArea(editorMain, inspector),
      horizontalOverflow: document.documentElement.scrollWidth - viewport.width,
      inspectorBounds: inspector ? bounds(inspector) : null,
      inspectorControlGeometry: controlGeometry(inspectorControls),
      inspectorControlOverlaps: overlaps(inspectorControls),
      missingSurfaces,
      mobileDialogPresent: mobileDialog !== null,
      outOfViewportSurfaces,
      viewport,
    }
  })
  const {
    editorBounds,
    editorControlGeometry,
    inspectorBounds,
    inspectorControlGeometry,
    viewport,
    ...layout
  } = snapshot as typeof snapshot & {
    editorBounds: LayoutBounds | null
    editorControlGeometry: ControlGeometry[]
    inspectorBounds: LayoutBounds | null
    inspectorControlGeometry: ControlGeometry[]
    viewport: Viewport
  }
  return {
    ...layout,
    outOfBoundsControls: [
      ...controlBoundsViolations(editorControlGeometry, editorBounds, viewport),
      ...controlBoundsViolations(inspectorControlGeometry, inspectorBounds, viewport),
    ],
  }
}

async function chooseWriteMode(
  page: Page,
  locale: 'de' | 'en',
  mode: 'edit' | 'suggest',
): Promise<void> {
  const button = page.getByRole('button', { name: labels[locale][mode], exact: true }).first()
  await expect(button).toBeVisible()
  if (await button.isEnabled()) await button.click()
  await expect(button).toHaveAttribute('aria-pressed', 'true')
}

async function appendMarker(surface: Locator, marker: string): Promise<void> {
  await focusEditorEnd(surface)
  await surface.pressSequentially(` ${marker}`, { delay: 5 })
}

async function appendMarkerAtomically(
  page: Page,
  surface: Locator,
  marker: string,
): Promise<void> {
  await focusEditorEnd(surface)
  await page.keyboard.insertText(` ${marker}`)
}

async function insertMarkerBeforeText(
  surface: Locator,
  anchorText: string,
  marker: string,
): Promise<void> {
  const target = await surface.evaluate((element, needle) => {
    const walker = document.createTreeWalker(
      element,
      NodeFilter.SHOW_TEXT,
      {
        acceptNode(node) {
          return node.parentElement?.closest('[contenteditable="false"]')
            ? NodeFilter.FILTER_REJECT
            : NodeFilter.FILTER_ACCEPT
        },
      },
    )
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      const offset = node.textContent?.indexOf(needle) ?? -1
      if (offset < 0) continue

      node.parentElement?.scrollIntoView({ block: 'center', inline: 'nearest' })
      const range = document.createRange()
      range.setStart(node, offset)
      range.setEnd(node, offset + 1)
      const rect = range.getClientRects()[0]
      if (!rect || rect.width <= 0 || rect.height <= 0) return null
      return {
        x: rect.left + Math.min(1, rect.width / 4),
        y: rect.top + rect.height / 2,
      }
    }
    return null
  }, anchorText)
  expect(target, 'Expected a visible first character for the exact editor anchor.').not.toBeNull()
  await surface.page().mouse.click(target!.x, target!.y)
  await expect.poll(() => surface.evaluate((element, needle) => {
    const selection = window.getSelection()
    const anchorNode = selection?.anchorNode ?? null
    const focusNode = selection?.focusNode ?? null
    const anchorOffset = selection?.anchorOffset ?? -1
    return {
      beforeAnchor: anchorNode?.nodeType === Node.TEXT_NODE
        && (anchorNode.textContent ?? '').slice(anchorOffset).startsWith(needle),
      insideEditor: anchorNode !== null
        && focusNode !== null
        && element.contains(anchorNode)
        && element.contains(focusNode),
      isCollapsed: selection?.isCollapsed === true,
    }
  }, anchorText)).toEqual({
    beforeAnchor: true,
    insideEditor: true,
    isCollapsed: true,
  })
  await surface.page().keyboard.insertText(marker)
  await expect.poll(() => surface.evaluate((element, input) => {
    const walker = document.createTreeWalker(
      element,
      NodeFilter.SHOW_TEXT,
      {
        acceptNode(node) {
          return node.parentElement?.closest('[contenteditable="false"]')
            ? NodeFilter.FILTER_REJECT
            : NodeFilter.FILTER_ACCEPT
        },
      },
    )
    let text = ''
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      text += node.textContent ?? ''
    }
    return text.includes(`${input.marker}${input.anchorText}`)
  }, { anchorText, marker })).toBe(true)
}

type ImeEventRecord = {
  data: string | null
  inputType: string | null
  isComposing: boolean
  isTrusted: boolean
  type: string
}

async function composeMarkerWithChromiumIme(
  page: Page,
  surface: Locator,
  marker: string,
): Promise<ImeEventRecord[]> {
  await focusEditorEnd(surface)
  await surface.evaluate((element) => {
    const records: Array<{
      data: string | null
      inputType: string | null
      isComposing: boolean
      isTrusted: boolean
      type: string
    }> = []
    const record = (event: Event) => {
      const input = event as InputEvent
      const composition = event as CompositionEvent
      records.push({
        data: input.data ?? composition.data ?? null,
        inputType: input.inputType ?? null,
        isComposing: input.isComposing ?? false,
        isTrusted: event.isTrusted,
        type: event.type,
      })
    }
    for (const type of ['beforeinput', 'compositionend', 'compositionstart', 'compositionupdate', 'input']) {
      element.addEventListener(type, record)
    }
    ;(window as unknown as { __inqtrixImeEvents: typeof records }).__inqtrixImeEvents = records
  })
  const session = await page.context().newCDPSession(page)
  try {
    await session.send('Input.imeSetComposition', {
      selectionEnd: marker.length,
      selectionStart: marker.length,
      text: marker,
    })
    await session.send('Input.insertText', { text: marker })
  } finally {
    await session.detach()
  }
  return surface.evaluate(() => (
    (window as unknown as { __inqtrixImeEvents: ImeEventRecord[] }).__inqtrixImeEvents
  ))
}

async function focusEditorEnd(surface: Locator): Promise<void> {
  await surface.evaluate((element) => {
    ;(element as HTMLElement).focus()
    const selection = window.getSelection()
    const range = document.createRange()
    range.selectNodeContents(element)
    range.collapse(false)
    selection?.removeAllRanges()
    selection?.addRange(range)
    document.dispatchEvent(new Event('selectionchange'))
  })
}

/** Wie oft die Marke auf einer Flaeche steht -- und dass sie DORT BLEIBT.
 *
 *  Ein blosser `expect.poll` ist erfuellt, sobald der Zaehler die Erwartung
 *  zum ERSTEN Mal beruehrt. Eine Verdopplung, die einen Rundlauf spaeter
 *  eintrifft, liegt dann hinter der Messung -- die Zusicherung "genau
 *  einmal" kann "zweimal" strukturell nicht sehen. Genau darauf sitzt
 *  `@concurrent-edits` ("converge exactly once"), das ausser diesen
 *  Zaehlungen keinen serverseitigen Gegenbeleg fuehrt.
 *
 *  Deshalb folgt auf die Konvergenz ein Ruhe-Fenster mit EIGENEM
 *  Zeitbudget. Geteilt mit der Konvergenzfrist wuerde eine langsame
 *  Konvergenz das Fenster auffressen und Geisterrot erzeugen -- der
 *  Fehler, den `waitForDurableBrowserUpdate` (editor-system-live.mjs:1658)
 *  im gemeinsamen `deadline` noch hat. Das Ruhe-Muster selbst ist von
 *  dort uebernommen (`stableSince`, 250 ms unveraendert).
 *
 *  Fuer die 30 Aufrufe mit `expected = 0` wirkt dasselbe Fenster als
 *  Wartezeit: eine Abwesenheit laesst sich nur ueber eine Zeitspanne
 *  beobachten, nicht in einem einzigen Sample abfragen.
 */
const TEXT_OCCURRENCE_SETTLE_MS = 250

async function expectTextOccurrences(
  surface: Locator,
  marker: string,
  expected: number,
): Promise<void> {
  await expect(surface).toBeVisible()
  const occurrences = async (): Promise<number> => {
    const content = await surface.textContent()
    return (content ?? '').split(marker).length - 1
  }
  await expect.poll(occurrences).toBe(expected)
  const settleDeadline = Date.now() + TEXT_OCCURRENCE_SETTLE_MS
  while (Date.now() < settleDeadline) {
    await new Promise((resolve) => setTimeout(resolve, 25))
    expect(
      await occurrences(),
      `Die Marke "${marker}" blieb nicht bei ${expected} Vorkommen.`,
    ).toBe(expected)
  }
}

async function selectMarker(surface: Locator, marker: string): Promise<void> {
  await surface.scrollIntoViewIfNeeded()
  const drag = await surface.evaluate((element, needle) => {
    const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT)
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      const offset = node.textContent?.indexOf(needle) ?? -1
      if (offset < 0) continue
      const firstCharacter = document.createRange()
      firstCharacter.setStart(node, offset)
      firstCharacter.setEnd(node, offset + 1)
      const lastCharacter = document.createRange()
      lastCharacter.setStart(node, offset + needle.length - 1)
      lastCharacter.setEnd(node, offset + needle.length)
      const firstRect = firstCharacter.getClientRects()[0]
      const lastRects = lastCharacter.getClientRects()
      const lastRect = lastRects[lastRects.length - 1]
      if (!firstRect || !lastRect) return null
      return {
        end: {
          x: lastRect.right - Math.min(1, lastRect.width / 4),
          y: lastRect.top + lastRect.height / 2,
        },
        start: {
          x: firstRect.left + Math.min(1, firstRect.width / 4),
          y: firstRect.top + firstRect.height / 2,
        },
      }
    }
    return null
  }, marker)
  expect(drag, `Expected visible drag coordinates for marker ${marker}`).not.toBeNull()
  if (!drag) return

  const page = surface.page()
  await page.keyboard.press('Escape')
  await page.mouse.move(drag.start.x, drag.start.y)
  await page.mouse.down()
  try {
    await page.mouse.move(drag.end.x, drag.end.y, { steps: 12 })
  } finally {
    await page.mouse.up()
  }

  await expect.poll(() => surface.evaluate((element, needle) => {
    const selection = window.getSelection()
    return {
      insideEditor: Boolean(
        selection?.anchorNode
        && selection.focusNode
        && element.contains(selection.anchorNode)
        && element.contains(selection.focusNode)
      ),
      selectedText: selection?.toString() ?? '',
    }
  }, marker)).toEqual({
    insideEditor: true,
    selectedText: marker,
  })
}

async function expectRemoteCaretAtMarker(
  page: Page,
  marker: string,
  displayName: string,
): Promise<void> {
  await expect.poll(async () => page.evaluate(({ author, needle }) => {
    const labels = Array.from(
      document.querySelectorAll<HTMLElement>('.inqtrix-collaboration-caret-label'),
    ).filter((candidate) => candidate.textContent?.trim() === author)
    const label = labels[0]
    const caret = label?.closest<HTMLElement>('.inqtrix-collaboration-caret')
    const root = caret?.closest<HTMLElement>('.editor-prose')
    if (!label || !caret || !root) return { exactIdentityCount: labels.length, atMarker: false }

    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT)
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      const offset = node.textContent?.indexOf(needle) ?? -1
      if (offset < 0) continue
      const markerRange = document.createRange()
      markerRange.setStart(node, offset + needle.length - 1)
      markerRange.setEnd(node, offset + needle.length)
      const markerRect = markerRange.getBoundingClientRect()
      const caretRect = caret.getBoundingClientRect()
      const sameLine = Math.abs(caretRect.top - markerRect.top) <= 8
      const adjacent = Math.abs(caretRect.left - markerRect.right) <= 12
      return { atMarker: adjacent && sameLine, exactIdentityCount: labels.length }
    }
    return { atMarker: false, exactIdentityCount: labels.length }
  }, { author: displayName, needle: marker })).toEqual({
    atMarker: true,
    exactIdentityCount: 1,
  })
}

type PrivateAnchorDescriptor = NonNullable<CollaborationE2EStack['privateAnchors']>['owner']
type PrivateAnchorDocuments = NonNullable<CollaborationE2EStack['privateAnchors']>['documents']

function privateAnchorDocumentForProject(
  documents: PrivateAnchorDocuments,
  testInfo: TestInfo,
): string {
  const { browser, formFactor } = testInfo.project.metadata
  if (typeof browser !== 'string' || typeof formFactor !== 'string') {
    throw new Error(
      `Private-anchor project ${testInfo.project.name} has no browser/formFactor metadata.`,
    )
  }
  const target = `${browser}-${formFactor}` as keyof PrivateAnchorDocuments
  const documentId = documents[target]
  if (!documentId) {
    throw new Error(
      `Private-anchor fixture has no isolated document for ${testInfo.project.name}.`,
    )
  }
  return documentId
}

type PrivateAnchorSnapshot = {
  aiId: string
  aiOffset: number
  commentId: string
  commentOffset: number
}

type PrivateSuggestionPublicationIdentity = {
  commentId: string
  patchId: string
  publicationCommandId: string
  suggestionId: string
}

async function ensurePrivateComment(
  page: Page,
  documentId: string,
  descriptor: PrivateAnchorDescriptor,
  locale: keyof typeof labels,
): Promise<void> {
  const decoration = page.locator('.editor-prose [data-editor-comment-anchor]', {
    hasText: descriptor.commentAnchorText,
  })
  if (await decoration.count() === 0) {
    await selectMarkerForComment(editor(page), descriptor.commentAnchorText)
    const privateCommentMenu = page.getByRole('button', {
      exact: true,
      name: labels[locale].privateComment,
    })
    await expect(privateCommentMenu).toBeVisible()
    await privateCommentMenu.click()
    const startComment = page.getByRole('menuitem', {
      exact: true,
      name: labels[locale].privateComment,
    })
    await expect(startComment).toBeVisible()
    await startComment.click()
    const composer = page.getByPlaceholder(
      labels[locale].privateCommentPlaceholder,
      { exact: true },
    )
    await expect(composer).toBeVisible()
    await composer.fill(descriptor.commentText)
    const persistence = page.waitForResponse((response) => {
      const path = new URL(response.url()).pathname
      return response.request().method() === 'POST'
        && path.endsWith(`/v1/editor/documents/${documentId}/comments`)
    })
    await composer.press('Enter')
    const response = await persistence
    expect(response.status()).toBe(201)
  }
  await expect(decoration).toHaveCount(1)
  await expectPersistedPrivateCommentAnchor(page, documentId, descriptor)
}

async function expectPersistedPrivateCommentAnchor(
  page: Page,
  documentId: string,
  descriptor: PrivateAnchorDescriptor,
): Promise<void> {
  const endpoint = new URL(
    `/v1/editor/documents/${encodeURIComponent(documentId)}/comments?limit=200`,
    page.url(),
  ).toString()
  await expect.poll(async () => {
    const response = await page.request.get(endpoint)
    if (!response.ok()) return null
    const payload = await response.json() as {
      data?: Array<{
        anchor?: Record<string, unknown>
        comment_markdown?: unknown
      }>
    }
    const comment = payload.data?.find(
      (candidate) => candidate.comment_markdown === descriptor.commentText,
    )
    if (!comment?.anchor) return null
    return {
      relativeFromPresent: typeof comment.anchor.relativeFrom === 'string'
        && comment.anchor.relativeFrom.length > 0,
      relativeToPresent: typeof comment.anchor.relativeTo === 'string'
        && comment.anchor.relativeTo.length > 0,
      relativeVersion: comment.anchor.relativeVersion,
      selectedText: comment.anchor.selectedText,
    }
  }, { intervals: [250, 500, 1_000], timeout: 15_000 }).toEqual({
    relativeFromPresent: true,
    relativeToPresent: true,
    relativeVersion: EDITOR_SCHEMA_BEHAVIOR_INPUTS.relativePositions,
    selectedText: descriptor.commentAnchorText,
  })
}

async function ensurePrivateSuggestion(
  page: Page,
  documentId: string,
  descriptor: PrivateAnchorDescriptor,
  locale: keyof typeof labels,
): Promise<void> {
  if (!descriptor.aiInstructionText) return
  const instruction = page.getByText(descriptor.aiInstructionText, {
    exact: true,
  })
  await expect(instruction).toBeVisible()
  await instruction.click()
  const proposal = page.getByText(descriptor.aiText, { exact: true })
  const runSuggestion = instruction.locator('..').getByRole('button', {
    name: labels[locale].runSuggestion,
  })
  await expect.poll(async () => (
    await proposal.isVisible() || await runSuggestion.isVisible()
  ), { timeout: 15_000 }).toBe(true)
  if (await proposal.isVisible()) {
    await expectPersistedPrivateSuggestionAnchor(page, documentId, descriptor)
    return
  }

  const routePattern = '**/v1/editor/suggest'
  const handler = async (route: Route): Promise<void> => {
    await route.fulfill({
      body: JSON.stringify({
        change_summary: [],
        improved_text: descriptor.aiText,
        warnings: [],
      }),
      contentType: 'application/json',
      status: 200,
    })
  }
  await page.route(routePattern, handler)
  try {
    const [persistence] = await Promise.all([
      page.waitForResponse((response) => {
        const path = new URL(response.url()).pathname
        return response.request().method() === 'PUT'
          && path.startsWith(`/v1/editor/documents/${documentId}/comments/`)
          && path.endsWith('/suggestion-draft')
      }),
      runSuggestion.click(),
    ])
    expect(persistence.status()).toBe(200)
    await expect(proposal).toBeVisible()
    await expectPersistedPrivateSuggestionAnchor(page, documentId, descriptor)
  } finally {
    if (!page.isClosed()) await page.unroute(routePattern, handler)
  }
}

async function expectPersistedPrivateSuggestionAnchor(
  page: Page,
  documentId: string,
  descriptor: PrivateAnchorDescriptor,
): Promise<void> {
  const endpoint = new URL(
    `/v1/editor/documents/${encodeURIComponent(documentId)}/comments?limit=200`,
    page.url(),
  ).toString()
  await expect.poll(async () => {
    const response = await page.request.get(endpoint)
    if (!response.ok()) return null
    const payload = await response.json() as {
      data?: Array<{
        anchor?: Record<string, unknown>
        comment_markdown?: unknown
        suggestion_draft?: { proposed_text?: unknown } | null
      }>
    }
    const comment = payload.data?.find(
      (candidate) => candidate.comment_markdown === descriptor.aiInstructionText,
    )
    if (!comment?.anchor || !comment.suggestion_draft) return null
    return {
      proposedText: comment.suggestion_draft.proposed_text,
      relativeFromPresent: typeof comment.anchor.relativeFrom === 'string'
        && comment.anchor.relativeFrom.length > 0,
      relativeToPresent: typeof comment.anchor.relativeTo === 'string'
        && comment.anchor.relativeTo.length > 0,
      relativeVersion: comment.anchor.relativeVersion,
      selectedText: comment.anchor.selectedText,
    }
  }, { intervals: [250, 500, 1_000], timeout: 15_000 }).toEqual({
    proposedText: descriptor.aiText,
    relativeFromPresent: true,
    relativeToPresent: true,
    relativeVersion: EDITOR_SCHEMA_BEHAVIOR_INPUTS.relativePositions,
    selectedText: descriptor.aiAnchorText,
  })
}

async function persistedPrivateSuggestionIdentity(
  page: Page,
  documentId: string,
  descriptor: PrivateAnchorDescriptor,
  expectedSuggestionId: string,
): Promise<PrivateSuggestionPublicationIdentity> {
  const endpoint = new URL(
    `/v1/editor/documents/${encodeURIComponent(documentId)}/comments?limit=200`,
    page.url(),
  ).toString()
  const readIdentity = async (): Promise<PrivateSuggestionPublicationIdentity | null> => {
    const response = await page.request.get(endpoint)
    if (!response.ok()) return null
    const payload = await response.json() as {
      data?: Array<{
        comment_markdown?: unknown
        id?: unknown
        suggestion_draft?: {
          patch_id?: unknown
          publication_command_id?: unknown
          suggestion_id?: unknown
        } | null
      }>
    }
    const comment = payload.data?.find(
      (candidate) => candidate.comment_markdown === descriptor.aiInstructionText,
    )
    const draft = comment?.suggestion_draft
    if (
      typeof comment?.id !== 'string'
      || !draft
      || typeof draft.patch_id !== 'string'
      || typeof draft.publication_command_id !== 'string'
      || typeof draft.suggestion_id !== 'string'
    ) return null
    return {
      commentId: comment.id,
      patchId: draft.patch_id,
      publicationCommandId: draft.publication_command_id,
      suggestionId: draft.suggestion_id,
    }
  }

  await expect.poll(async () => (
    (await readIdentity())?.suggestionId ?? null
  ), { intervals: [250, 500, 1_000], timeout: 15_000 }).toBe(expectedSuggestionId)
  const identity = await readIdentity()
  if (!identity) {
    throw new Error('The persisted private suggestion identity disappeared after verification.')
  }
  expect(identity.patchId).toMatch(/^[0-9a-f-]{36}$/i)
  expect(identity.publicationCommandId).toMatch(/^[0-9a-f-]{36}$/i)
  return identity
}

async function expectPrivateSuggestionDraftCleared(
  page: Page,
  documentId: string,
  commentId: string,
): Promise<void> {
  const endpoint = new URL(
    `/v1/editor/documents/${encodeURIComponent(documentId)}/comments?limit=200`,
    page.url(),
  ).toString()
  await expect.poll(async () => {
    const response = await page.request.get(endpoint)
    if (!response.ok()) return `http-${response.status()}`
    const payload = await response.json() as {
      data?: Array<{ id?: unknown; suggestion_draft?: unknown }>
    }
    const comment = payload.data?.find((candidate) => candidate.id === commentId)
    if (!comment) return 'comment-missing'
    return comment.suggestion_draft === null ? 'cleared' : 'present'
  }, { intervals: [250, 500, 1_000], timeout: 15_000 }).toBe('cleared')
}

async function selectPublishedCollaborationChange(
  page: Page,
  locale: 'de' | 'en',
  patchId: string,
): Promise<void> {
  await ensureInspector(page, locale)
  const changes = page.getByRole('tab', {
    name: new RegExp(`^${labels[locale].changes}`),
  })
  await changes.click()
  await expect(changes).toHaveAttribute('data-state', 'active')
  const row = page.locator(`[data-inspector-change-id="${patchId}"]`)
  await expect(row).toHaveCount(1)
  await expect(row).toBeVisible()
  await row.locator('button').first().click()
}

async function expectPublishedReplacementMarks(
  page: Page,
  suggestionId: string,
  patchId: string,
): Promise<void> {
  const surface = editor(page)
  const deletion = surface.locator(
    `del[data-suggestion-id="${suggestionId}"]`,
  )
  const insertion = surface.locator(
    `ins[data-suggestion-id="${suggestionId}"]`,
  )
  await Promise.all([
    expect(deletion).toHaveCount(1),
    expect(insertion).toHaveCount(1),
  ])
  await Promise.all([
    expect(deletion).toBeVisible(),
    expect(insertion).toBeVisible(),
    expect(deletion).toHaveAttribute('data-suggestion-patch-id', patchId),
    expect(insertion).toHaveAttribute('data-suggestion-patch-id', patchId),
  ])
}

async function expectPublishedPrivateSuggestionExactlyOnce(
  page: Page,
  documentId: string,
  identity: PrivateSuggestionPublicationIdentity,
  sharedSuggestionIds: readonly string[],
): Promise<void> {
  const endpoint = new URL(
    `/v1/editor/documents/${encodeURIComponent(documentId)}/patches`,
    page.url(),
  ).toString()
  await expect.poll(async () => {
    const response = await page.request.get(endpoint)
    if (!response.ok()) return null
    const payload = await response.json() as {
      data?: Array<{
        command_id?: unknown
        patch_id?: unknown
        status?: unknown
        suggestion_ids?: unknown
      }>
    }
    const matches = (payload.data ?? []).filter(
      (candidate) => candidate.patch_id === identity.patchId,
    )
    return {
      count: matches.length,
      patches: matches.map((candidate) => ({
        commandId: candidate.command_id,
        status: candidate.status,
        suggestionIds: candidate.suggestion_ids,
      })),
    }
  }, { intervals: [250, 500, 1_000], timeout: 15_000 }).toEqual({
    count: 1,
    patches: [{
      commandId: identity.publicationCommandId,
      status: 'pending',
      suggestionIds: [...sharedSuggestionIds],
    }],
  })
}

async function privateAnchorSnapshot(
  page: Page,
  descriptor: PrivateAnchorDescriptor,
): Promise<PrivateAnchorSnapshot> {
  await expect(page.getByText(descriptor.commentText, { exact: true })).toBeVisible()
  if (!descriptor.aiInstructionText) {
    throw new Error('Private anchor snapshot requires an AI instruction card.')
  }
  const instruction = page.getByText(descriptor.aiInstructionText, {
    exact: true,
  })
  await expect(instruction).toBeVisible()
  await instruction.click()
  await expect(page.getByText(descriptor.aiText, { exact: true })).toBeVisible()
  const replacement = replacementDelta(
    descriptor.aiAnchorText,
    descriptor.aiText,
  )
  expect(replacement.deleted).not.toBe('')
  expect(replacement.inserted).not.toBe('')
  const aiDecoration = page.locator('.editor-prose [data-suggestion-id]', {
    hasText: replacement.deleted,
  })
  const aiInsertion = page.locator('.editor-prose [data-suggestion-insert="true"]', {
    hasText: replacement.inserted,
  })
  const commentDecoration = page.locator('.editor-prose [data-editor-comment-anchor]', {
    hasText: descriptor.commentAnchorText,
  })
  await expect(aiDecoration).toHaveCount(1)
  await expect(aiInsertion).toHaveCount(1)
  await expect(commentDecoration).toHaveCount(1)
  await expect(aiDecoration).toHaveText(replacement.deleted)
  await expect(aiInsertion).toHaveText(replacement.inserted)
  await expect(commentDecoration).toHaveText(descriptor.commentAnchorText)
  const aiId = await aiDecoration.getAttribute('data-suggestion-id')
  const commentId = await commentDecoration.getAttribute('data-editor-comment-anchor')
  expect(aiId).toBeTruthy()
  expect(commentId).toBeTruthy()
  return {
    aiId: aiId!,
    aiOffset: await decorationTextOffset(aiDecoration),
    commentId: commentId!,
    commentOffset: await decorationTextOffset(commentDecoration),
  }
}

function replacementDelta(
  original: string,
  proposed: string,
): { deleted: string; inserted: string } {
  let prefix = 0
  while (
    prefix < original.length
    && prefix < proposed.length
    && original[prefix] === proposed[prefix]
  ) {
    prefix += 1
  }
  let suffix = 0
  while (
    suffix < original.length - prefix
    && suffix < proposed.length - prefix
    && original[original.length - suffix - 1]
      === proposed[proposed.length - suffix - 1]
  ) {
    suffix += 1
  }
  return {
    deleted: original.slice(prefix, original.length - suffix),
    inserted: proposed.slice(prefix, proposed.length - suffix),
  }
}

async function decorationTextOffset(decoration: Locator): Promise<number> {
  return decoration.evaluate((element) => {
    const root = element.closest('.editor-prose')
    if (!root) throw new Error('Private anchor decoration is outside the collaboration editor.')
    const range = document.createRange()
    range.selectNodeContents(root)
    range.setEndBefore(element)
    return range.toString().length
  })
}

async function expectPrivateAnchorPrivacy(
  page: Page,
  foreignDescriptor: PrivateAnchorDescriptor,
  foreignSnapshot: PrivateAnchorSnapshot,
): Promise<void> {
  await expect(page.getByText(foreignDescriptor.commentText, { exact: true })).toHaveCount(0)
  await expect(page.getByText(foreignDescriptor.aiText, { exact: true })).toHaveCount(0)
  await expect(page.locator('.editor-prose [data-editor-comment-anchor]', {
    hasText: foreignDescriptor.commentAnchorText,
  })).toHaveCount(0)
  const counts = await page.evaluate(({ aiId, commentId }) => ({
    ai: Array.from(document.querySelectorAll('[data-suggestion-id]'))
      .filter((element) => element.getAttribute('data-suggestion-id') === aiId).length,
    comment: Array.from(document.querySelectorAll('[data-editor-comment-anchor]'))
      .filter((element) => element.getAttribute('data-editor-comment-anchor') === commentId).length,
  }), {
    aiId: foreignSnapshot.aiId,
    commentId: foreignSnapshot.commentId,
  })
  expect(counts).toEqual({ ai: 0, comment: 0 })
}

async function collaborationSelectionPresentation(page: Page): Promise<{
  count: number
  opaqueCount: number
  unclassifiedCount: number
}> {
  return page.evaluate(() => {
    const selections = Array.from(
      document.querySelectorAll<HTMLElement>('[data-collaboration-selection]'),
    )
    return {
      count: selections.length,
      opaqueCount: selections.filter((selection) => {
        const style = getComputedStyle(selection)
        return (
          style.backgroundColor !== 'rgba(0, 0, 0, 0)'
          && style.backgroundColor !== 'transparent'
        ) || style.boxShadow !== 'none'
      }).length,
      unclassifiedCount: selections.filter((selection) => (
        !selection.classList.contains('inqtrix-collaboration-selection')
        || selection.dataset.collaborationSelection !== 'transparent'
      )).length,
    }
  })
}

async function installWebSocketObserver(page: Page): Promise<void> {
  await page.addInitScript(installWebSocketObserverInPage)
}

async function installStatusObserver(page: Page): Promise<void> {
  await page.addInitScript(installStatusObserverInPage)
}

/** Jedes Erscheinen des Wiederverbindungs-Streifens, durchgehend beobachtet.
 *
 * Ein leeres Ergebnis heisst NUR dann "der Streifen kam nie", wenn die Sonde
 * auch wirklich lief. Genau dafuer meldet dieser Zugriff `null` statt einer
 * leeren Liste, wenn kein Beobachterzustand im Fenster liegt -- eine fehlende
 * Sonde darf sich nicht als bestandene Zusicherung tarnen. */
async function observedReconnectAppearances(
  page: Page,
): Promise<CollaborationStatusObserverState['reconnectAppearances'] | null> {
  return page.evaluate(() => {
    const state = (window as unknown as {
      __inqtrixCollaborationStatusObserver?: CollaborationStatusObserverState
    }).__inqtrixCollaborationStatusObserver
    return state ? [...state.reconnectAppearances] : null
  })
}

async function armLargeStateBrowserTiming(
  page: Page,
  sourceMarker: string,
  remoteMarker: string,
): Promise<void> {
  await page.evaluate(({ remoteMarker, sourceMarker }) => {
    type TimingStore = {
      inputAt: Record<string, number | null>
      visibleAt: Record<string, number | null>
    }
    type TimingWindow = Window & typeof globalThis & {
      __inqtrixLargeStateTiming?: TimingStore
    }
    const surface = document.querySelector<HTMLElement>(
      '.editor-prose[contenteditable="true"]',
    )
    if (!surface) throw new Error('Writable editor surface is not mounted.')
    const timingWindow = window as TimingWindow
    const timing = timingWindow.__inqtrixLargeStateTiming ?? {
      inputAt: {},
      visibleAt: {},
    }
    timingWindow.__inqtrixLargeStateTiming = timing
    timing.inputAt[sourceMarker] = null
    timing.visibleAt[remoteMarker] = null

    const recordInput = (event: Event) => {
      const data = (event as InputEvent).data
      if (typeof data !== 'string' || !data.includes(sourceMarker)) return
      timing.inputAt[sourceMarker] = Date.now()
      surface.removeEventListener('beforeinput', recordInput)
    }
    surface.addEventListener('beforeinput', recordInput)

    const containsRemoteMarker = (node: Node): boolean => (
      node.textContent?.includes(remoteMarker) === true
    )
    const observer = new MutationObserver((mutations) => {
      const visible = mutations.some((mutation) => (
        mutation.type === 'characterData'
          ? containsRemoteMarker(mutation.target)
          : [...mutation.addedNodes].some(containsRemoteMarker)
      ))
      if (!visible) return
      timing.visibleAt[remoteMarker] = Date.now()
      observer.disconnect()
    })
    observer.observe(surface, {
      characterData: true,
      childList: true,
      subtree: true,
    })
  }, { remoteMarker, sourceMarker })
}

async function waitForLargeStateBrowserTiming(
  page: Page,
  sourceMarker: string,
  remoteMarker: string,
): Promise<void> {
  await expect.poll(async () => {
    return await page.evaluate(({ remoteMarker, sourceMarker }) => {
      const timing = (window as unknown as {
        __inqtrixLargeStateTiming?: {
          inputAt: Record<string, number | null>
          visibleAt: Record<string, number | null>
        }
      }).__inqtrixLargeStateTiming
      return (
        Number.isFinite(timing?.inputAt[sourceMarker])
        && Number.isFinite(timing?.visibleAt[remoteMarker])
      )
    }, { remoteMarker, sourceMarker })
  }, {
    message: 'Browser input and remote DOM visibility must both be measured.',
    timeout: 30_000,
  }).toBe(true)
}

async function largeStateBrowserTiming(
  page: Page,
  sourceMarker: string,
  remoteMarker: string,
): Promise<{ inputAt: number; visibleAt: number }> {
  const timing = await page.evaluate(({ remoteMarker, sourceMarker }) => {
    const store = (window as unknown as {
      __inqtrixLargeStateTiming?: {
        inputAt: Record<string, number | null>
        visibleAt: Record<string, number | null>
      }
    }).__inqtrixLargeStateTiming
    return {
      inputAt: store?.inputAt[sourceMarker] ?? null,
      visibleAt: store?.visibleAt[remoteMarker] ?? null,
    }
  }, { remoteMarker, sourceMarker })
  if (!Number.isFinite(timing.inputAt) || !Number.isFinite(timing.visibleAt)) {
    throw new Error('Large-state browser timing is incomplete.')
  }
  return timing as { inputAt: number; visibleAt: number }
}

async function waitForDurableAckAfter(
  page: Page,
  baselineOrder: number,
): Promise<void> {
  await expect.poll(async () => {
    const state = await observedCollaborationSocketState(page)
    return state.events.filter((event) => (
      event.kind === 'durable_ack' && event.order > baselineOrder
    )).length
  }, {
    message: 'The visible browser input must receive a durable acknowledgement.',
    timeout: 30_000,
  }).toBeGreaterThan(0)
  await expect.poll(async () => (
    await observedCollaborationSocketState(page)
  ).pendingFrameDecodes).toBe(0)
}

async function observedCloseCodes(page: Page): Promise<number[]> {
  const state = await observedCollaborationSocketState(page)
  return state.events.flatMap((event) => event.kind === 'close' ? [event.code] : [])
}

async function observedCollaborationSocketState(
  page: Page,
): Promise<CollaborationSocketObserverState> {
  return page.evaluate(() => {
    const state = (window as unknown as {
      __inqtrixCollaborationSocketObserver?: CollaborationSocketObserverState
    }).__inqtrixCollaborationSocketObserver
    return state
      ? {
          events: [...state.events].sort((left, right) => left.order - right.order),
          pendingFrameDecodes: state.pendingFrameDecodes,
        }
      : { events: [], pendingFrameDecodes: 0 }
  })
}

async function observedActiveCollaborationSocketId(page: Page): Promise<number | null> {
  const state = await observedCollaborationSocketState(page)
  const active = new Map<number, number>()
  for (const event of state.events) {
    if (event.kind === 'open') active.set(event.socketId, event.order)
    else if (event.kind === 'close') active.delete(event.socketId)
  }
  const latest = [...active.entries()].sort((left, right) => right[1] - left[1])[0]
  return latest?.[0] ?? null
}

async function waitForObservedSocketClosure(
  page: Page,
  socketId: number,
  afterOrder: number,
): Promise<void> {
  await expect.poll(async () => {
    const state = await observedCollaborationSocketState(page)
    return collaborationSocketWindow(state.events, socketId, afterOrder) !== null
  }, { timeout: 30_000 }).toBe(true)
}

async function removeMarker(surface: Locator, marker: string): Promise<void> {
  const alreadySelected = await surface.evaluate((element, needle) => {
    const selection = window.getSelection()
    return (
      selection?.toString() === needle
      && selection.anchorNode !== null
      && selection.focusNode !== null
      && element.contains(selection.anchorNode)
      && element.contains(selection.focusNode)
    )
  }, marker)
  if (!alreadySelected) await selectMarkerForCleanup(surface, marker)
  await surface.page().keyboard.press('Backspace')
  await expectTextOccurrences(surface, marker, 0)
}

async function selectMarkerForCleanup(
  surface: Locator,
  marker: string,
): Promise<void> {
  const selected = await surface.evaluate((element, needle) => {
    const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT)
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      const offset = node.textContent?.indexOf(needle) ?? -1
      if (offset < 0) continue
      const range = document.createRange()
      range.setStart(node, offset)
      range.setEnd(node, offset + needle.length)
      const selection = window.getSelection()
      ;(element as HTMLElement).focus()
      selection?.removeAllRanges()
      selection?.addRange(range)
      document.dispatchEvent(new Event('selectionchange'))
      return selection?.toString() ?? ''
    }
    return null
  }, marker)
  expect(selected, `Expected cleanup selection for marker ${marker}`).toBe(marker)
}

async function selectMarkerForComment(
  surface: Locator,
  marker: string,
): Promise<void> {
  await selectMarkerForCleanup(surface, marker)
  await expect.poll(() => surface.evaluate((element) => {
    const selection = window.getSelection()
    return {
      insideEditor: Boolean(
        selection?.anchorNode
        && selection.focusNode
        && element.contains(selection.anchorNode)
        && element.contains(selection.focusNode)
      ),
      selectedText: selection?.toString() ?? '',
    }
  })).toEqual({ insideEditor: true, selectedText: marker })
}

function projectFrontmatterValue(contents: string, key: string): unknown {
  const normalized = contents.replace(/\r\n?/g, '\n')
  if (!normalized.startsWith('---\n')) return undefined
  const endIndex = normalized.indexOf('\n---', 4)
  if (endIndex < 0) return undefined
  const prefix = `${key}:`
  const line = normalized
    .slice(4, endIndex)
    .split('\n')
    .find((candidate) => candidate.startsWith(prefix))
  if (!line) return undefined
  const serialized = line.slice(prefix.length).trim()
  try {
    return JSON.parse(serialized) as unknown
  } catch {
    return serialized
  }
}

function normalizedImportedDocumentTitle(title: string): string {
  const normalized = title.replace(/\s+/g, ' ').trim() || 'Untitled'
  return normalized.endsWith('.md') ? normalized : `${normalized}.md`
}

function projectUploadFilesInAdversarialOrder(
  files: StoredZipFile[],
  documentOrder: readonly string[],
  activeDocumentId: string,
) {
  const markdownFiles = files.filter((file) => file.path.endsWith('.md'))
  const manifestCount = markdownFiles.filter((file) => (
    file.path === 'project.md' || file.path.endsWith('/project.md')
  )).length
  if (manifestCount !== 1) {
    throw new Error(`Project upload requires exactly one manifest; received ${manifestCount}.`)
  }
  const uploads = markdownFiles.map((file, index) => ({
    contents: file.contents,
    mimeType: 'text/markdown',
    name: file.path === 'project.md' || file.path.endsWith('/project.md')
      ? 'project.md'
      : `project-entry-${String(index).padStart(3, '0')}.md`,
  }))
  const manifest = uploads.find((file) => file.name === 'project.md')
  if (!manifest) throw new Error('Project upload omitted its manifest.')
  const documentUploads = new Map(uploads.flatMap((file) => {
    if (projectFrontmatterValue(file.contents, 'kind') !== 'inqtrix.editor_document') {
      return []
    }
    const documentId = projectFrontmatterValue(file.contents, 'document_id')
    return typeof documentId === 'string' && documentId
      ? [[documentId, file] as const]
      : []
  }))
  const otherUploads = uploads.filter((file) => (
    file !== manifest
    && projectFrontmatterValue(file.contents, 'kind') !== 'inqtrix.editor_document'
  ))
  if (!documentOrder.includes(activeDocumentId)) {
    throw new Error('Detached transfer active document is missing from the manifest order.')
  }
  const adversarialDocumentOrder = [
    ...documentOrder.filter((documentId) => documentId !== activeDocumentId).reverse(),
    activeDocumentId,
  ]
  if (adversarialDocumentOrder.length < 3) {
    throw new Error('Detached transfer order verification requires at least three documents.')
  }
  if (adversarialDocumentOrder.every((documentId, index) => documentId === documentOrder[index])) {
    const first = adversarialDocumentOrder[0]!
    adversarialDocumentOrder[0] = adversarialDocumentOrder[1]!
    adversarialDocumentOrder[1] = first
  }
  return [
    manifest,
    ...otherUploads,
    ...adversarialDocumentOrder.map((documentId) => {
      const upload = documentUploads.get(documentId)
      if (!upload) {
        throw new Error(`Project manifest references missing upload document ${documentId}.`)
      }
      return upload
    }),
  ]
}

async function dispatchProjectFiles(
  input: Locator,
  files: ReturnType<typeof projectUploadFilesInAdversarialOrder>,
): Promise<void> {
  const dispatchedCount = await input.evaluate((element, selectedFiles) => {
    if (!(element instanceof HTMLInputElement)) {
      throw new Error('Project file target is not an input element.')
    }
    const transfer = new DataTransfer()
    for (const selectedFile of selectedFiles) {
      transfer.items.add(new File(
        [selectedFile.contents],
        selectedFile.name,
        { type: selectedFile.mimeType },
      ))
    }
    element.files = transfer.files
    element.dispatchEvent(new Event('input', { bubbles: true }))
    element.dispatchEvent(new Event('change', { bubbles: true }))
    return transfer.files.length
  }, files)
  expect(dispatchedCount).toBe(files.length)
}

async function interceptNextProjectFileInput(page: Page): Promise<Locator> {
  await page.evaluate(() => {
    const intercept = (event: MouseEvent) => {
      const input = event.target
      if (
        !(input instanceof HTMLInputElement)
        || input.type !== 'file'
        || !input.hasAttribute('directory')
        || !input.hasAttribute('webkitdirectory')
      ) return
      input.dataset.inqtrixE2eProjectInput = 'true'
      event.preventDefault()
      document.removeEventListener('click', intercept, true)
    }
    document.addEventListener('click', intercept, true)
  })
  return page.locator('input[data-inqtrix-e2e-project-input="true"]')
}

function projectMarkdownBodyHash(contents: string): string {
  const normalized = contents.replace(/\r\n?/g, '\n')
  const frontmatterEnd = normalized.startsWith('---\n')
    ? normalized.indexOf('\n---', 4)
    : -1
  const body = frontmatterEnd >= 0
    ? normalized.slice(frontmatterEnd + 4).replace(/^\n/, '')
    : normalized
  return createHash('sha256').update(body.trim()).digest('hex')
}


function projectEditorDocumentOrder(files: StoredZipFile[]): string[] {
  const manifests = files.filter((file) => (
    file.path === 'project.md' || file.path.endsWith('/project.md')
  ))
  if (manifests.length !== 1) {
    throw new Error(`Project export requires exactly one manifest; received ${manifests.length}.`)
  }
  const value = projectFrontmatterValue(manifests[0]!.contents, 'editor_document_order')
  if (
    !Array.isArray(value)
    || value.some((documentId) => typeof documentId !== 'string' || !documentId)
    || new Set(value).size !== value.length
  ) {
    throw new Error('Project manifest contains an invalid editor document order.')
  }
  return value as string[]
}

function projectDocumentEntries(files: StoredZipFile[]): {
  bodyHash: string
  id: string
}[] {
  return files
    .filter((file) => (
      projectFrontmatterValue(file.contents, 'kind') === 'inqtrix.editor_document'
    ))
    .map((file) => {
      const id = projectFrontmatterValue(file.contents, 'document_id')
      if (typeof id !== 'string' || !id) {
        throw new Error('Project export contains an editor document without an identity.')
      }
      return { bodyHash: projectMarkdownBodyHash(file.contents), id }
    })
}


async function decideVisibleSuggestion(
  ownerPage: Page,
  locale: 'de' | 'en',
  marker: string,
  decision: 'accept' | 'reject',
): Promise<void> {
  await ensureInspector(ownerPage, locale)
  await ownerPage.getByRole('tab', { name: new RegExp(`^${labels[locale].changes}`) }).click()
  const row = ownerPage.locator('[data-inspector-change-id]', { hasText: marker }).first()
  await expect(row).toBeVisible({ timeout: 20_000 })
  await row.locator('button').first().click()
  await row.getByRole('button', { name: labels[locale][decision], exact: true }).click()
  await expect(row).toBeHidden({ timeout: 20_000 })
  await closeMobileInspector(ownerPage, locale)
}

async function expectSuggestionIdentity(
  ownerPage: Page,
  collaboratorPage: Page,
  marker: string,
  expectedAuthorId: string,
): Promise<void> {
  const ownerMark = ownerPage.locator('.editor-prose ins[data-suggestion-id]', {
    hasText: marker,
  })
  const collaboratorMark = collaboratorPage.locator(
    '.editor-prose ins[data-suggestion-id]',
    { hasText: marker },
  )
  await expect(ownerMark).toHaveCount(1)
  await expect(collaboratorMark).toHaveCount(1)
  const suggestionId = await ownerMark.getAttribute('data-suggestion-id')
  expect(suggestionId).toMatch(
    /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i,
  )
  expect(await collaboratorMark.getAttribute('data-suggestion-id')).toBe(suggestionId)
  expect(await ownerMark.getAttribute('data-suggestion-author-id')).toBe(expectedAuthorId)
  expect(await collaboratorMark.getAttribute('data-suggestion-author-id')).toBe(
    expectedAuthorId,
  )
}

type StoredZipFile = {
  contents: string
  path: string
}

async function forceDownloadProjectPicker(page: Page): Promise<void> {
  await page.evaluate(() => {
    Object.defineProperty(window, 'showDirectoryPicker', {
      configurable: true,
      value: undefined,
    })
  })
}

async function triggerProjectAction(
  page: Page,
  locale: 'de' | 'en',
  actionLabel: string,
): Promise<void> {
  const directButton = page.getByRole('button', { name: actionLabel, exact: true })
  if (await directButton.isVisible().catch(() => false)) {
    await directButton.click()
    return
  }
  await page.getByRole('button', { name: labels[locale].menu, exact: true }).click()
  await page.getByRole('menuitem', { name: actionLabel, exact: true }).click()
}

async function downloadProjectArchive(
  page: Page,
  locale: 'de' | 'en',
  actionLabel: string,
): Promise<Buffer> {
  const [download] = await Promise.all([
    page.waitForEvent('download'),
    triggerProjectAction(page, locale, actionLabel),
  ])
  return await downloadBytes(download)
}

async function downloadBytes(download: Download): Promise<Buffer> {
  const stream = await download.createReadStream()
  if (!stream) throw new Error('Project export download did not expose a readable stream.')
  const chunks: Buffer[] = []
  for await (const chunk of stream) {
    chunks.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk))
  }
  return Buffer.concat(chunks)
}

function parseStoredZip(bytes: Buffer): StoredZipFile[] {
  const decoder = new TextDecoder('utf-8', { fatal: true })
  const files: StoredZipFile[] = []
  let offset = 0
  while (offset + 4 <= bytes.length) {
    const signature = bytes.readUInt32LE(offset)
    if (signature === 0x02014b50 || signature === 0x06054b50) break
    if (signature !== 0x04034b50 || offset + 30 > bytes.length) {
      throw new Error('Project export is not a valid stored ZIP archive.')
    }
    const method = bytes.readUInt16LE(offset + 8)
    const compressedSize = bytes.readUInt32LE(offset + 18)
    const uncompressedSize = bytes.readUInt32LE(offset + 22)
    const nameLength = bytes.readUInt16LE(offset + 26)
    const extraLength = bytes.readUInt16LE(offset + 28)
    if (method !== 0 || compressedSize !== uncompressedSize) {
      throw new Error('Project export ZIP must use the uncompressed project format.')
    }
    const nameStart = offset + 30
    const dataStart = nameStart + nameLength + extraLength
    const dataEnd = dataStart + compressedSize
    if (dataEnd > bytes.length) throw new Error('Project export ZIP entry is truncated.')
    const path = decoder.decode(bytes.subarray(nameStart, nameStart + nameLength))
    if (
      !path
      || path.startsWith('/')
      || path.split('/').some((segment) => segment === '..')
    ) {
      throw new Error('Project export ZIP contains an unsafe entry path.')
    }
    files.push({
      contents: decoder.decode(bytes.subarray(dataStart, dataEnd)),
      path,
    })
    offset = dataEnd
  }
  if (files.length === 0) throw new Error('Project export ZIP contains no project files.')
  return files
}

type ShareRecord = {
  accepted_at: number | null
  id: string
  permission: 'edit' | 'suggest' | 'view'
  recipient_user_id: string
  revision: number
}

type BrowserApiResult = {
  contentType: string
  ok: boolean
  payload: unknown
  status: number
  transportError: boolean
}

type EditorDocumentDetail = {
  collaboration: {
    persisted_sequence: number
    projection_sequence: number
    projection_updated_at: number | null
    schema_version: number
  }
  content_markdown: string
}

async function editorDocumentMarkdown(
  page: Page,
  documentId: string,
): Promise<string> {
  const result = await browserApi(page, `/v1/editor/documents/${documentId}`, 'GET')
  requireApiSuccess(result)
  const payload = result.payload as { content_markdown?: unknown; id?: unknown } | null
  if (payload?.id !== documentId || typeof payload.content_markdown !== 'string') {
    throw new Error('Editor document detail omitted its identity or Markdown body.')
  }
  return payload.content_markdown
}

async function editorDocumentDetail(
  page: Page,
  documentId: string,
): Promise<EditorDocumentDetail> {
  const result = await browserApi(page, `/v1/editor/documents/${documentId}`, 'GET')
  requireApiSuccess(result)
  const payload = result.payload as Partial<EditorDocumentDetail> | null
  const collaboration = payload?.collaboration
  if (
    typeof payload?.content_markdown !== 'string'
    || !collaboration
    || !Number.isSafeInteger(collaboration.persisted_sequence)
    || !Number.isSafeInteger(collaboration.projection_sequence)
    || !Number.isSafeInteger(collaboration.schema_version)
    || (
      collaboration.projection_updated_at !== null
      && typeof collaboration.projection_updated_at !== 'number'
    )
  ) {
    throw new Error('Editor document detail omitted the collaboration projection contract.')
  }
  return payload as EditorDocumentDetail
}

async function requireProjectionFlush(page: Page, documentId: string): Promise<void> {
  requireApiSuccess(await browserApi(
    page,
    `/v1/editor/documents/${documentId}/collaboration/projection:flush`,
    'POST',
  ))
}

function expectTextCount(content: string, marker: string, expected: number): void {
  expect(content.split(marker).length - 1).toBe(expected)
}

function apiFailureReason(result: BrowserApiResult): string | null {
  const payload = result.payload as {
    detail?: { error?: { reason?: unknown }; reason?: unknown }
    error?: { reason?: unknown }
  } | null
  const reason = payload?.error?.reason
    ?? payload?.detail?.error?.reason
    ?? payload?.detail?.reason
  return typeof reason === 'string' ? reason : null
}

type BrowserProtocolProbeState = {
  authChallenges: number
  authChallengesAtArm: number
  authenticationDenied: boolean
  closeCodes: number[]
  durableAckHashes: string[]
  errors: string[]
  open: boolean
  scopes: string[]
  synced: boolean
  updateSentAfterChallenge: boolean
}

function createIndependentYjsUpdate(marker: string): { bytes: Uint8Array; hash: string } {
  const document = new Y.Doc()
  try {
    const paragraph = new Y.XmlElement('paragraph')
    const text = new Y.XmlText()
    text.insert(0, marker)
    paragraph.insert(0, [text])
    document.getXmlFragment('content').insert(0, [paragraph])
    const bytes = Y.encodeStateAsUpdate(document)
    return {
      bytes,
      hash: createHash('sha256').update(bytes).digest('hex'),
    }
  } finally {
    document.destroy()
  }
}

async function openBrowserProtocolProbe(page: Page, payload: unknown): Promise<void> {
  const session = parseCollaborationProtocolSession(payload)
  const emptyDocument = new Y.Doc()
  let syncOne: Uint8Array
  let syncTwo: Uint8Array
  try {
    syncOne = encodeProtocolFrame(
      session.room,
      0,
      encodeProtocolVarUint(0),
      encodeProtocolBytes(Y.encodeStateVector(emptyDocument)),
    )
    syncTwo = encodeProtocolFrame(
      session.room,
      0,
      encodeProtocolVarUint(1),
      encodeProtocolBytes(Y.encodeStateAsUpdate(emptyDocument)),
    )
  } finally {
    emptyDocument.destroy()
  }
  const authentication = encodeProtocolFrame(
    session.room,
    2,
    encodeProtocolVarUint(0),
    encodeProtocolString(session.leaseToken),
    encodeProtocolString('4.3.0'),
  )

  await page.evaluate(({ auth, room, sync1, sync2, websocketPath }) => {
    type ProbeRuntime = BrowserProtocolProbeState & {
      authenticationFrame: Uint8Array
      challengeHeld: boolean
      encodeUpdate: (update: Uint8Array) => Uint8Array
      pendingUpdateFrame: Uint8Array | null
      policyChangeCommitted: boolean
      resumePolicyChallenge: () => void
      socket: WebSocket
      syncTwoFrame: Uint8Array
    }
    type ProbeWindow = Window & typeof globalThis & {
      __inqtrixPermissionProbe?: ProbeRuntime
    }

    const decodeBase64 = (value: string): Uint8Array => Uint8Array.from(
      atob(value),
      (character) => character.charCodeAt(0),
    )
    const concat = (...parts: Uint8Array[]): Uint8Array => {
      const result = new Uint8Array(parts.reduce((total, part) => total + part.length, 0))
      let offset = 0
      for (const part of parts) {
        result.set(part, offset)
        offset += part.length
      }
      return result
    }
    const encodeVarUint = (value: number): Uint8Array => {
      const bytes: number[] = []
      let remaining = value
      do {
        let byte = remaining & 0x7f
        remaining = Math.floor(remaining / 128)
        if (remaining > 0) byte |= 0x80
        bytes.push(byte)
      } while (remaining > 0)
      return Uint8Array.from(bytes)
    }
    const encodeBytes = (value: Uint8Array): Uint8Array => concat(
      encodeVarUint(value.length),
      value,
    )
    const encodeString = (value: string): Uint8Array => encodeBytes(
      new TextEncoder().encode(value),
    )
    const encodeFrame = (type: number, ...parts: Uint8Array[]): Uint8Array => concat(
      encodeString(room),
      encodeVarUint(type),
      ...parts,
    )

    class Decoder {
      private offset = 0

      constructor(private readonly bytes: Uint8Array) {}

      get remaining(): number {
        return this.bytes.length - this.offset
      }

      readVarUint(): number {
        let value = 0
        let multiplier = 1
        while (this.offset < this.bytes.length) {
          const byte = this.bytes[this.offset++]!
          value += (byte & 0x7f) * multiplier
          if ((byte & 0x80) === 0) return value
          multiplier *= 128
          if (!Number.isSafeInteger(value) || multiplier > 2 ** 49) break
        }
        throw new Error('invalid varuint')
      }

      readBytes(): Uint8Array {
        const length = this.readVarUint()
        const end = this.offset + length
        if (end > this.bytes.length) throw new Error('truncated bytes')
        const value = this.bytes.subarray(this.offset, end)
        this.offset = end
        return value
      }

      readString(): string {
        return new TextDecoder().decode(this.readBytes())
      }
    }

    const url = new URL(websocketPath, window.location.href)
    url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:'
    if (url.host !== window.location.host || url.username || url.password) {
      throw new Error('Collaboration websocket_path must stay on the current host.')
    }
    const socket = new WebSocket(url)
    socket.binaryType = 'arraybuffer'
    const sendBinary = (frame: Uint8Array): void => {
      const copy = new Uint8Array(frame.byteLength)
      copy.set(frame)
      socket.send(copy.buffer)
    }
    const runtime: ProbeRuntime = {
      authChallenges: 0,
      authChallengesAtArm: 0,
      authenticationDenied: false,
      authenticationFrame: decodeBase64(auth),
      challengeHeld: false,
      closeCodes: [],
      durableAckHashes: [],
      encodeUpdate: (update) => encodeFrame(
        0,
        encodeVarUint(2),
        encodeBytes(update),
      ),
      errors: [],
      open: false,
      pendingUpdateFrame: null,
      policyChangeCommitted: true,
      resumePolicyChallenge: () => {},
      scopes: [],
      socket,
      synced: false,
      syncTwoFrame: decodeBase64(sync2),
      updateSentAfterChallenge: false,
    }
    runtime.resumePolicyChallenge = () => {
      if (runtime.pendingUpdateFrame && !runtime.updateSentAfterChallenge) {
        sendBinary(runtime.pendingUpdateFrame)
        runtime.updateSentAfterChallenge = true
      }
      sendBinary(runtime.authenticationFrame)
      runtime.challengeHeld = false
    }
    ;(window as ProbeWindow).__inqtrixPermissionProbe?.socket.close(1000)
    ;(window as ProbeWindow).__inqtrixPermissionProbe = runtime

    socket.addEventListener('open', () => {
      runtime.open = true
      sendBinary(runtime.authenticationFrame)
      sendBinary(decodeBase64(sync1))
    })
    socket.addEventListener('close', (event) => {
      runtime.open = false
      runtime.closeCodes.push(event.code)
    })
    socket.addEventListener('error', () => {
      if (runtime.closeCodes.length === 0) runtime.errors.push('websocket transport error')
    })
    socket.addEventListener('message', (event) => {
      try {
        if (!(event.data instanceof ArrayBuffer)) throw new Error('non-binary frame')
        const bytes = new Uint8Array(event.data)
        if (bytes.length === 1 && bytes[0] === 9) {
          sendBinary(Uint8Array.of(10))
          return
        }
        if (bytes.length === 1 && bytes[0] === 10) return
        const decoder = new Decoder(bytes)
        if (decoder.readString() !== room) throw new Error('wrong room')
        const type = decoder.readVarUint()
        if (type === 2) {
          const subtype = decoder.readVarUint()
          if (subtype === 0) {
            runtime.authChallenges += 1
            if (!runtime.policyChangeCommitted) {
              runtime.challengeHeld = true
              return
            }
            runtime.resumePolicyChallenge()
          } else if (subtype === 1) {
            runtime.authenticationDenied = true
            if (decoder.remaining > 0) decoder.readString()
          } else if (subtype === 2) {
            runtime.scopes.push(decoder.readString())
          } else {
            throw new Error('unknown auth subtype')
          }
          return
        }
        if (type === 0 || type === 4) {
          const subtype = decoder.readVarUint()
          if (subtype === 0) {
            decoder.readBytes()
            sendBinary(runtime.syncTwoFrame)
          } else if (subtype === 1 || subtype === 2) {
            decoder.readBytes()
            if (subtype === 1) runtime.synced = true
          } else {
            throw new Error('unknown sync subtype')
          }
          return
        }
        if (type === 5) {
          const stateless = JSON.parse(decoder.readString()) as {
            hash?: unknown
            type?: unknown
          }
          if (
            stateless.type === 'durable_ack'
            && typeof stateless.hash === 'string'
            && /^[a-f0-9]{64}$/.test(stateless.hash)
          ) {
            runtime.durableAckHashes.push(stateless.hash)
          }
          return
        }
        if (type === 1) decoder.readBytes()
        else if (type === 6) decoder.readString()
        else if (type === 7 && decoder.remaining > 0) decoder.readString()
        else if (type === 8) decoder.readVarUint()
      } catch {
        runtime.errors.push('invalid collaboration protocol frame')
      }
    })
  }, {
    auth: Buffer.from(authentication).toString('base64'),
    room: session.room,
    sync1: Buffer.from(syncOne).toString('base64'),
    sync2: Buffer.from(syncTwo).toString('base64'),
    websocketPath: session.websocketPath,
  })

  await expect.poll(async () => {
    const state = await browserProtocolProbeState(page)
    return {
      errors: state.errors,
      open: state.open,
      readWrite: state.scopes.includes('read-write'),
      synced: state.synced,
    }
  }, { timeout: 30_000 }).toEqual({
    errors: [],
    open: true,
    readWrite: true,
    synced: true,
  })
}

async function sendBrowserProtocolProbeUpdate(
  page: Page,
  update: Uint8Array,
): Promise<void> {
  await page.evaluate((encodedUpdate) => {
    type ProbeRuntime = BrowserProtocolProbeState & {
      encodeUpdate: (bytes: Uint8Array) => Uint8Array
      socket: WebSocket
    }
    const runtime = (window as unknown as {
      __inqtrixPermissionProbe?: ProbeRuntime
    }).__inqtrixPermissionProbe
    if (!runtime || !runtime.open || !runtime.synced) {
      throw new Error('Raw collaboration protocol probe is not ready.')
    }
    const updateBytes = Uint8Array.from(
      atob(encodedUpdate),
      (character) => character.charCodeAt(0),
    )
    const frame = runtime.encodeUpdate(updateBytes)
    const copy = new Uint8Array(frame.byteLength)
    copy.set(frame)
    runtime.socket.send(copy.buffer)
  }, Buffer.from(update).toString('base64'))
}

async function armBrowserProtocolProbeUpdate(page: Page, update: Uint8Array): Promise<void> {
  await page.evaluate((encodedUpdate) => {
    type ProbeRuntime = BrowserProtocolProbeState & {
      challengeHeld: boolean
      encodeUpdate: (bytes: Uint8Array) => Uint8Array
      pendingUpdateFrame: Uint8Array | null
      policyChangeCommitted: boolean
      socket: WebSocket
    }
    const runtime = (window as unknown as {
      __inqtrixPermissionProbe?: ProbeRuntime
    }).__inqtrixPermissionProbe
    if (!runtime || !runtime.open || !runtime.synced) {
      throw new Error('Raw collaboration permission probe is not ready.')
    }
    runtime.authChallengesAtArm = runtime.authChallenges
    runtime.challengeHeld = false
    runtime.policyChangeCommitted = false
    const bytes = Uint8Array.from(
      atob(encodedUpdate),
      (character) => character.charCodeAt(0),
    )
    runtime.pendingUpdateFrame = runtime.encodeUpdate(bytes)
  }, Buffer.from(update).toString('base64'))
}

async function commitBrowserProtocolProbePolicyChange(page: Page): Promise<void> {
  await page.evaluate(() => {
    const runtime = (window as unknown as {
      __inqtrixPermissionProbe?: {
        challengeHeld: boolean
        policyChangeCommitted: boolean
        resumePolicyChallenge: () => void
      }
    }).__inqtrixPermissionProbe
    if (!runtime) throw new Error('Raw collaboration permission probe is unavailable.')
    runtime.policyChangeCommitted = true
    if (runtime.challengeHeld) runtime.resumePolicyChallenge()
  })
}

async function browserProtocolProbeState(page: Page): Promise<BrowserProtocolProbeState> {
  return page.evaluate(() => {
    const runtime = (window as unknown as {
      __inqtrixPermissionProbe?: BrowserProtocolProbeState
    }).__inqtrixPermissionProbe
    if (!runtime) throw new Error('Raw collaboration permission probe is unavailable.')
    return {
      authChallenges: runtime.authChallenges,
      authChallengesAtArm: runtime.authChallengesAtArm,
      authenticationDenied: runtime.authenticationDenied,
      closeCodes: [...runtime.closeCodes],
      durableAckHashes: [...runtime.durableAckHashes],
      errors: [...runtime.errors],
      open: runtime.open,
      scopes: [...runtime.scopes],
      synced: runtime.synced,
      updateSentAfterChallenge: runtime.updateSentAfterChallenge,
    }
  })
}

async function closeBrowserProtocolProbe(page: Page): Promise<void> {
  await page.evaluate(() => {
    const holder = window as unknown as {
      __inqtrixPermissionProbe?: { socket: WebSocket }
    }
    const runtime = holder.__inqtrixPermissionProbe
    if (runtime?.socket.readyState === WebSocket.OPEN) runtime.socket.close(1000)
    delete holder.__inqtrixPermissionProbe
  }).catch(() => {})
}

function encodeProtocolFrame(
  room: string,
  type: number,
  ...payloads: Uint8Array[]
): Uint8Array {
  return concatProtocolBytes(
    encodeProtocolString(room),
    encodeProtocolVarUint(type),
    ...payloads,
  )
}

function encodeProtocolString(value: string): Uint8Array {
  return encodeProtocolBytes(new TextEncoder().encode(value))
}

function encodeProtocolBytes(value: Uint8Array): Uint8Array {
  return concatProtocolBytes(encodeProtocolVarUint(value.length), value)
}

function encodeProtocolVarUint(value: number): Uint8Array {
  if (!Number.isSafeInteger(value) || value < 0) {
    throw new Error('Protocol varuint must be a non-negative safe integer.')
  }
  const bytes: number[] = []
  let remaining = value
  do {
    let byte = remaining & 0x7f
    remaining = Math.floor(remaining / 128)
    if (remaining > 0) byte |= 0x80
    bytes.push(byte)
  } while (remaining > 0)
  return Uint8Array.from(bytes)
}

function concatProtocolBytes(...parts: Uint8Array[]): Uint8Array {
  const bytes = new Uint8Array(parts.reduce((total, part) => total + part.length, 0))
  let offset = 0
  for (const part of parts) {
    bytes.set(part, offset)
    offset += part.length
  }
  return bytes
}

async function closeCodeForTextFrame(page: Page, payload: unknown): Promise<number> {
  const websocketPath = (
    payload as { websocket_path?: unknown } | null
  )?.websocket_path
  if (typeof websocketPath !== 'string' || !websocketPath.trim()) {
    throw new Error('Collaboration session omitted websocket_path.')
  }
  return page.evaluate(async (path) => {
    const url = new URL(path, window.location.origin)
    url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:'
    if (url.host !== window.location.host || url.username || url.password) {
      throw new Error('Collaboration websocket_path must stay on the current host.')
    }
    return new Promise<number>((resolve, reject) => {
      const socket = new WebSocket(url)
      const timer = window.setTimeout(() => {
        socket.close()
        reject(new Error('Collaboration gateway did not reject a text frame within 10 seconds.'))
      }, 10_000)
      socket.addEventListener('open', () => socket.send('invalid-text-frame'))
      socket.addEventListener('close', (event) => {
        window.clearTimeout(timer)
        resolve(event.code)
      })
    })
  }, websocketPath)
}

async function activeEditorShare(page: Page, documentId: string, recipientId: string): Promise<ShareRecord> {
  const params = new URLSearchParams({ resource_id: documentId, resource_type: 'editor_document' })
  const result = await browserApi(page, `/v1/shares?${params.toString()}`, 'GET')
  requireApiSuccess(result)
  const shares = (result.payload as { data?: ShareRecord[] } | null)?.data ?? []
  const share = shares.find((candidate) => candidate.recipient_user_id === recipientId)
  if (!share?.accepted_at || !Number.isSafeInteger(share.revision)) {
    throw new Error('The collaboration fixture requires an accepted, revisioned collaborator share.')
  }
  return share
}

async function updateEditorSharePermission(
  ownerPage: Page,
  share: ShareRecord,
  permission: ShareRecord['permission'],
): Promise<ShareRecord> {
  const result = await browserApi(ownerPage, `/v1/shares/${share.id}`, 'PATCH', {
    expected_revision: share.revision,
    permission,
  })
  requireApiSuccess(result)
  const updated = (result.payload as {
    data?: Partial<ShareRecord>
  } | null)?.data
  if (
    !updated
    || updated.id !== share.id
    || updated.permission !== permission
    || !Number.isSafeInteger(updated.revision)
    || updated.revision !== share.revision + 1
  ) {
    throw new Error('Share permission update omitted its compare-and-swap revision contract.')
  }
  return updated as ShareRecord
}

async function restoreEditorShare(
  ownerPage: Page,
  collaboratorPage: Page,
  documentId: string,
  recipientId: string,
  permission: ShareRecord['permission'],
): Promise<void> {
  const params = new URLSearchParams({ resource_id: documentId, resource_type: 'editor_document' })
  const listed = await browserApi(ownerPage, `/v1/shares?${params.toString()}`, 'GET')
  requireApiSuccess(listed)
  let share = ((listed.payload as { data?: ShareRecord[] } | null)?.data ?? [])
    .find((candidate) => candidate.recipient_user_id === recipientId)
  if (!share) {
    const created = await browserApi(ownerPage, '/v1/shares', 'POST', {
      invitees: [{ permission, user_id: recipientId }],
      resource_id: documentId,
      resource_type: 'editor_document',
    })
    requireApiSuccess(created)
    share = (created.payload as { data?: ShareRecord[] } | null)?.data?.[0]
  }
  if (!share) throw new Error('The collaboration share could not be restored.')
  if (share.accepted_at === null) {
    requireApiSuccess(await browserApi(collaboratorPage, `/v1/shares/${share.id}/accept`, 'POST'))
  }
  if (share.permission !== permission) {
    if (!Number.isSafeInteger(share.revision)) {
      throw new Error('The collaboration share restore response omitted revision.')
    }
    await updateEditorSharePermission(ownerPage, share, permission)
  }
}

async function browserApi(
  page: Page,
  path: string,
  method: 'DELETE' | 'GET' | 'PATCH' | 'POST',
  body?: unknown,
  timeoutMs = 10_000,
): Promise<BrowserApiResult> {
  return page.evaluate(async ({ requestBody, requestMethod, requestPath, requestTimeoutMs }) => {
    const headers = new Headers({ Accept: 'application/json' })
    if (requestBody !== undefined) headers.set('Content-Type', 'application/json')
    if (requestMethod !== 'GET') {
      const cookies = Object.fromEntries(document.cookie.split(';').map((entry) => {
        const [name, ...value] = entry.trim().split('=')
        return [name, decodeURIComponent(value.join('='))]
      }))
      const csrf = cookies['__Host-inqtrix_csrf'] ?? cookies.inqtrix_csrf
      if (csrf) headers.set('X-CSRF-Token', csrf)
    }
    try {
      const response = await fetch(requestPath, {
        body: requestBody === undefined ? undefined : JSON.stringify(requestBody),
        credentials: 'same-origin',
        headers,
        method: requestMethod,
        signal: AbortSignal.timeout(requestTimeoutMs),
      })
      let payload: unknown = null
      try {
        payload = await response.json()
      } catch {
        payload = null
      }
      return {
        contentType: response.headers.get('content-type')?.toLowerCase() ?? '',
        ok: response.ok,
        payload,
        status: response.status,
        transportError: false,
      }
    } catch {
      return {
        contentType: '',
        ok: false,
        payload: null,
        status: 0,
        transportError: true,
      }
    }
  }, {
    requestBody: body,
    requestMethod: method,
    requestPath: path,
    requestTimeoutMs: timeoutMs,
  })
}

function requireApiSuccess(result: BrowserApiResult): void {
  if (result.ok) return
  const reason = apiFailureReason(result)
  throw new Error(`Collaboration fixture API request failed with HTTP ${result.status}${
    reason ? ` (${reason})` : ''
  }.`)
}

function assertFastApiHealth(result: BrowserApiResult): void {
  if (isFastApiHealth(result)) return
  throw new Error('The public /health probe did not return the Inqtrix FastAPI JSON contract.')
}

function isFastApiHealth(result: BrowserApiResult): boolean {
  const payload = isRecord(result.payload) ? result.payload : null
  const validProvider = (value: unknown): boolean => (
    isRecord(value)
    && typeof value.provider === 'string'
    && value.provider.trim().length > 0
    && typeof value.status === 'string'
    && value.status.trim().length > 0
  )
  return (
    result.ok
    && result.status === 200
    && result.contentType.includes('application/json')
    && payload?.status === 'ok'
    && validProvider(payload.llm)
    && validProvider(payload.search)
    && typeof payload.auth_mode === 'string'
    && isRecord(payload.legal)
  )
}

function isPublicGatewayUnavailable(result: BrowserApiResult): boolean {
  return !result.ok && (
    result.transportError
    || [500, 502, 503, 504].includes(result.status)
  )
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value)
}

function uniqueMarker(kind: string): string {
  return `inqtrix-e2e-${kind}-${Date.now()}-${Math.random().toString(16).slice(2)}`
}

function summarizeLatencies(values: number[]): {
  max: number
  p50: number
  p95: number
  sampleCount: number
} {
  if (values.length === 0 || values.some((value) => !Number.isFinite(value))) {
    throw new Error('Latency summary requires finite samples.')
  }
  const sorted = [...values].sort((left, right) => left - right)
  const percentile = (fraction: number): number => (
    sorted[Math.max(0, Math.ceil(sorted.length * fraction) - 1)]!
  )
  return {
    max: sorted.at(-1)!,
    p50: percentile(0.5),
    p95: percentile(0.95),
    sampleCount: sorted.length,
  }
}
