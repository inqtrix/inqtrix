import '@fontsource-variable/inter/index.css'
import '@/styles/globals.css'

import { useState } from 'react'
import { createRoot } from 'react-dom/client'

import { AppProviders } from '@/app/AppProviders'
import { DocumentReader } from '../KnowledgeSourcePanel'
import type { DocumentViewerTarget, KnowledgeDataSource } from '../types'

const DOCUMENT_ID = 'kdoc-verordnung'

const ARTICLE_6 = 'Artikel 6 regelt die Einstufung von Hochrisiko-Systemen.'
const ARTICLE_43 = 'Artikel 43 regelt die Konformitätsbewertung.'

/** Two citations of ONE document, as a knowledge answer cites it twice. */
const TARGETS: Record<string, DocumentViewerTarget> = {
  K1: {
    chunkIndex: 0,
    documentId: DOCUMENT_ID,
    excerpt: ARTICLE_6,
    highlightTargets: [ARTICLE_6],
    title: 'KI-Verordnung',
  },
  K3: {
    chunkIndex: 5,
    documentId: DOCUMENT_ID,
    excerpt: ARTICLE_43,
    highlightTargets: [ARTICLE_43],
    title: 'KI-Verordnung',
  },
}

let loads = 0

/** A live deployment's data source: it probes file access (so the reader
 * loads the document as soon as a citation opens) and counts every document
 * text request on the body for the test to read. */
const dataSource: KnowledgeDataSource = {
  canLoadFileContent: () => Promise.resolve(false),
  loadDocumentText: (documentId) => {
    loads += 1
    document.body.dataset.documentLoads = String(loads)
    return Promise.resolve({
      chunk_count: 6,
      collection_id: 'col-verordnung',
      created_at: 0,
      id: documentId,
      metadata: {},
      text: `${ARTICLE_6}\n\nAnhang III nennt die Bereiche.\n\n${ARTICLE_43}`,
      title: 'KI-Verordnung',
    })
  },
  loadFileContent: null,
  search: () => Promise.resolve({ data: [], warnings: [] }),
}

function DocumentReaderFixture() {
  const [label, setLabel] = useState('K1')
  return (
    <div className="flex h-screen flex-col">
      <div className="flex gap-2 p-2">
        {Object.keys(TARGETS).map((key) => (
          <button key={key} onClick={() => setLabel(key)} type="button">
            {`Beleg ${key}`}
          </button>
        ))}
      </div>
      <div className="flex min-h-0 flex-1 flex-col" data-testid="reader">
        <DocumentReader dataSource={dataSource} target={TARGETS[label]} />
      </div>
    </div>
  )
}

createRoot(document.getElementById('root')!).render(
  <AppProviders>
    <DocumentReaderFixture />
  </AppProviders>,
)
