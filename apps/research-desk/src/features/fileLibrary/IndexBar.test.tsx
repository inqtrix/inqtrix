import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'

import { TooltipProvider } from '@/components/ui/tooltip'
import { LocaleProvider } from '@/i18n/LocaleProvider'
import type { IndexingJobLive, VectorIndexRecord, VectorIndexStatus } from '@/features/project/types'
import { IndexBar } from './IndexBar'

const index: VectorIndexRecord = {
  createdAt: '2026-07-22T08:00:00.000Z',
  dims: 3,
  handle: 'contracts',
  id: 'index-1',
  members: [],
  model: 'text-embedding-3-small',
  serverCollectionId: 'collection-1',
  status: 'indexing',
  title: 'Contracts',
  updatedAt: '2026-07-22T08:00:00.000Z',
}

// One hour ahead: the planned automatic resume reads as a clock time.
const PLAN_AT_SECONDS = Math.floor(Date.now() / 1000) + 3600
const planTime = new Date(PLAN_AT_SECONDS * 1000).toLocaleTimeString('de', {
  hour: '2-digit',
  minute: '2-digit',
})

const paused: IndexingJobLive = {
  autoResume: { attempts: 1, max: 5, next_at: PLAN_AT_SECONDS, stopped: null },
  completedDocuments: 2,
  currentBatch: 7,
  jobId: 'job-1',
  pauseErrorType: 'contextualization_provider_timeout',
  percent: 40,
  phase: 'contextualization',
  runningFileIds: [],
  source: 'server',
  startedAt: '2026-07-22T08:00:00.000Z',
  status: 'paused_dependency',
  totalBatches: 18,
  totalDocuments: 5,
}

describe('IndexBar paused indexing contract', () => {
  it('labels the client-derived vector count as an estimate', () => {
    const markup = renderToStaticMarkup(
      <LocaleProvider initialLocale="de">
        <TooltipProvider>
          <IndexBar
            embedModels={[{
              dims: 3,
              id: 'text-embedding-3-small',
              label: 'text-embedding-3-small',
              provider: 'OpenAI',
            }]}
            index={index}
            members={[]}
            onCancel={vi.fn()}
            onDelete={vi.fn()}
            onModel={vi.fn()}
            onReindex={vi.fn()}
            onResume={vi.fn()}
            onResumeRaw={vi.fn()}
            serverBacked
          />
        </TooltipProvider>
      </LocaleProvider>,
    )

    expect(markup).toContain('Vektoren (geschätzt)')
    expect(markup).toContain('Clientseitige Schätzung aus Seitenzahl oder Dateigröße')
    expect(markup).not.toMatch(/>Vektoren</)
  })

  it('keeps the running progress rail at a stable width independent of the document title', () => {
    const live: IndexingJobLive = {
      ...paused,
      currentDocumentTitle: 'A very long document title that must not resize the progress bar.pdf',
      phase: 'contextualization',
      source: 'server',
      status: 'running',
    }
    const markup = renderToStaticMarkup(
      <LocaleProvider initialLocale="de">
        <TooltipProvider>
          <IndexBar
            embedModels={[{
              dims: 3,
              id: 'text-embedding-3-small',
              label: 'text-embedding-3-small',
              provider: 'OpenAI',
            }]}
            index={index}
            live={live}
            members={[]}
            onCancel={vi.fn()}
            onDelete={vi.fn()}
            onModel={vi.fn()}
            onReindex={vi.fn()}
            onResume={vi.fn()}
            onResumeRaw={vi.fn()}
            serverBacked
          />
        </TooltipProvider>
      </LocaleProvider>,
    )

    expect(markup).toContain('md:flex-nowrap')
    expect(markup).toContain('md:w-48')
    expect(markup).toContain('truncate text-right')
  })

  it('keeps the active generation explicit and exposes both deliberate recovery paths', () => {
    const markup = renderToStaticMarkup(
      <LocaleProvider initialLocale="de">
        <TooltipProvider>
          <IndexBar
            embedModels={[{
              dims: 3,
              id: 'text-embedding-3-small',
              label: 'text-embedding-3-small',
              provider: 'OpenAI',
            }]}
            index={index}
            live={paused}
            members={[]}
            onCancel={vi.fn()}
            onDelete={vi.fn()}
            onModel={vi.fn()}
            onReindex={vi.fn()}
            onResume={vi.fn()}
            onResumeRaw={vi.fn()}
            serverBacked
          />
        </TooltipProvider>
      </LocaleProvider>,
    )

    expect(markup).toContain('Pausiert · Abhängigkeit')
    expect(markup).toContain('Der Anbieter für die Kontextanreicherung antwortet nicht.')
    expect(markup).toContain(`Nächster automatischer Versuch um ${planTime} (2/5)`)
    // The pause names its phase in the words of the document row.
    expect(markup).toContain('Kontextualisierung · Batch 7 von 18')
    expect(markup).not.toContain('contextualization')
    expect(markup).toContain('Aktiver Index unverändert')
    expect(markup).toContain('Fortsetzen')
    expect(markup).toContain('Ohne Kontext neu aufbauen')
    expect(markup).not.toContain('inqtrix-running-dot')
  })

  it.each([
    [true, 'zerlegt, kontextualisiert und eingebettet', 'Kontextanreicherung ist in dieser Umgebung deaktiviert'],
    [false, 'Kontextanreicherung ist in dieser Umgebung deaktiviert', 'zerlegt, kontextualisiert und eingebettet'],
  ] as const)(
    'describes the actual contextual-retrieval capability when it is %s',
    (contextualRetrievalEnabled, expected, rejected) => {
      const live: IndexingJobLive = {
        ...paused,
        phase: 'embedding',
        source: 'build',
        status: 'running',
      }
      const markup = renderToStaticMarkup(
        <LocaleProvider initialLocale="de">
          <TooltipProvider>
            <IndexBar
              contextualRetrievalEnabled={contextualRetrievalEnabled}
              embedModels={[{
                dims: 3,
                id: 'text-embedding-3-small',
                label: 'text-embedding-3-small',
                provider: 'OpenAI',
              }]}
              index={index}
              live={live}
              members={[]}
              onCancel={vi.fn()}
              onDelete={vi.fn()}
              onModel={vi.fn()}
              onReindex={vi.fn()}
              onResume={vi.fn()}
              onResumeRaw={vi.fn()}
              serverBacked
            />
          </TooltipProvider>
        </LocaleProvider>,
      )

      expect(markup).toContain(expected)
      expect(markup).not.toContain(rejected)
    },
  )

  it('offers no build without context when the vector store is what failed', () => {
    const markup = renderBar({ ...paused, pauseErrorType: 'vector_store_unavailable' })

    expect(markup).toContain('Der Vektorspeicher ist vorübergehend nicht erreichbar.')
    expect(markup).toContain('Fortsetzen')
    expect(markup).not.toContain('Ohne Kontext neu aufbauen')
  })

  it('names an ended streak and leaves the decision to a person', () => {
    const markup = renderBar({
      ...paused,
      autoResume: { attempts: 5, max: 5, next_at: null, stopped: 'exhausted' },
    })

    expect(markup).toContain('Automatische Versuche beendet (5/5), kein Fortschritt.')
    expect(markup).toContain('Bitte manuell fortsetzen oder abbrechen.')
    expect(markup).not.toContain('Nächster automatischer Versuch')
  })

  it('never shows a planned time that has already passed', () => {
    const markup = renderBar({
      ...paused,
      autoResume: { attempts: 1, max: 5, next_at: PLAN_AT_SECONDS - 7200, stopped: null },
    })

    expect(markup).toContain('Nächster automatischer Versuch in Kürze (2/5)')
  })
})

function renderBar(live: IndexingJobLive): string {
  return renderToStaticMarkup(
    <LocaleProvider initialLocale="de">
      <TooltipProvider>
        <IndexBar
          embedModels={[{
            dims: 3,
            id: 'text-embedding-3-small',
            label: 'text-embedding-3-small',
            provider: 'OpenAI',
          }]}
          index={index}
          live={live}
          members={[]}
          onCancel={vi.fn()}
          onDelete={vi.fn()}
          onModel={vi.fn()}
          onReindex={vi.fn()}
          onResume={vi.fn()}
          onResumeRaw={vi.fn()}
          serverBacked
        />
      </TooltipProvider>
    </LocaleProvider>,
  )
}

describe('IndexBar without a knowledge base behind it', () => {
  const localIndex: VectorIndexRecord = {
    ...index,
    serverCollectionId: undefined,
    status: 'ready',
  }

  function markupFor(
    unavailableReason?: string | null,
    status: VectorIndexStatus = 'ready',
  ): string {
    return renderToStaticMarkup(
      <LocaleProvider initialLocale="de">
        <TooltipProvider>
          <IndexBar
            embedModels={[{
              dims: 3,
              id: 'text-embedding-3-small',
              label: 'text-embedding-3-small',
              provider: 'OpenAI',
            }]}
            index={{ ...localIndex, status }}
            members={[]}
            onCancel={vi.fn()}
            onDelete={vi.fn()}
            onModel={vi.fn()}
            onReindex={vi.fn()}
            onResume={vi.fn()}
            onResumeRaw={vi.fn()}
            serverBacked={false}
            unavailableReason={unavailableReason}
          />
        </TooltipProvider>
      </LocaleProvider>,
    )
  }

  const disabledButton = (label: string) => new RegExp(
    `<button[^>]*disabled=""[^>]*>(?:(?!</button>)[\\s\\S])*${label}</button>`,
  )
  const disabledBuild = disabledButton('Aktualisieren')

  it('names a knowledge base the operator switched off and builds nothing', () => {
    const markup = markupFor('operator_disabled')

    expect(markup).toContain('Wissensbasis abgeschaltet')
    expect(markup).toContain('Die Wissensbasis ist in diesem Betrieb abgeschaltet.')
    expect(markup).not.toContain('>Demo<')
    expect(markup).toMatch(disabledBuild)
  })

  it('names an unreachable vector store and builds nothing', () => {
    const markup = markupFor('store_unreachable')

    expect(markup).toContain('Vektorspeicher nicht erreichbar')
    expect(markup).not.toContain('>Demo<')
    expect(markup).toMatch(disabledBuild)
  })

  it('keeps the demo label and its build in demo mode', () => {
    const markup = markupFor(null)

    expect(markup).toContain('>Demo<')
    expect(markup).not.toMatch(disabledBuild)
  })

  it.each([
    ['error', 'Erneut versuchen'],
    ['stale', 'Jetzt'],
  ] as const)('offers no build from a stored %s state', (status, action) => {
    // The status survives a reload; its own action would start the demo
    // simulator in a deployment that cannot build.
    expect(markupFor('operator_disabled', status)).toMatch(disabledButton(action))

    const demo = markupFor(null, status)
    expect(demo).toContain(`>${action}</button>`)
    expect(demo).not.toMatch(disabledButton(action))
  })
})
