import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import type { AdminSystemRuntime } from '@/api/inqtrixClient'
import { TooltipProvider } from '@/components/ui/tooltip'
import { LocaleProvider } from '@/i18n/LocaleProvider'
import { SystemStatusPanel } from './SystemStatusPanel'

function runtimeFixture(
  overrides: {
    api?: Partial<AdminSystemRuntime['api']>
    agents?: Partial<AdminSystemRuntime['agents']>
    runs?: Partial<AdminSystemRuntime['runs']>
    observability?: Partial<AdminSystemRuntime['observability']>
  } = {},
): AdminSystemRuntime {
  return {
    api: {
      openapi: false,
      chat_max_concurrent: 100,
      stream_reader_workers: 128,
      ...overrides.api,
    },
    files: {
      blob_storage: 'postgres',
      document_parser: 'markitdown',
      enabled: true,
      max_file_bytes: null,
      object_store: 'local',
      object_store_available: true,
    },
    knowledge: {
      contextual_retrieval: false,
      default_top_k: null,
      embedding_model: null,
      embedding_provider: null,
      enabled: false,
      hybrid_retrieval: false,
      reranker: 'none',
      sparse: null,
      vector_store: 'memory',
      vector_store_available: true,
    },
    runs: {
      execution: 'in_process',
      queue: 'memory',
      queue_available: true,
      queue_consumers: null,
      queue_depth: null,
      store: 'postgres',
      worker_dispatch: false,
      admission_max_concurrent: 100,
      queue_max_size: 100,
      ...overrides.runs,
    },
    agents: { checkpointer_pool_size: 4, ...overrides.agents },
    storage: { backend: 'postgres', durable: true },
    observability: {
      tracing: 'off',
      tracing_active: false,
      content_capture: false,
      sample_rate: 1,
      spool: false,
      retention_enforced: false,
      retention_days: null,
      ui_link_configured: false,
      ...overrides.observability,
    },
  }
}

function renderPanel(runtime: AdminSystemRuntime): string {
  return renderToStaticMarkup(
    <LocaleProvider initialLocale="de">
      <TooltipProvider>
        <SystemStatusPanel
          capabilities={null}
          health={null}
          runtime={runtime}
          runtimeError={null}
          runtimeStatus="ready"
        />
      </TooltipProvider>
    </LocaleProvider>,
  )
}

describe('SystemStatusPanel queue telemetry', () => {
  it('warns when the dispatch queue has no recently active consumers', () => {
    const markup = renderPanel(
      runtimeFixture({
        runs: {
          execution: 'worker_dispatch',
          queue: 'valkey',
          queue_consumers: 0,
          queue_depth: 3,
          worker_dispatch: true,
        },
        observability: { retention_enforced: true },
      }),
    )
    expect(markup).toContain('Keine Konsumenten')
    expect(markup).toContain('3 Nachrichten')
  })

  it('stays silent about consumers for in-process execution', () => {
    const markup = renderPanel(runtimeFixture())
    expect(markup).not.toContain('Konsumenten')
  })
})

describe('SystemStatusPanel files', () => {
  it('shows the parser that prepares uploads while knowledge is off', () => {
    const markup = renderPanel(runtimeFixture())
    const parser = /Dokumenten-Parser[\s\S]*?<span class="t-mono text-foreground">([^<]*)<\/span>/
      .exec(markup)?.[1]

    expect(parser).toBe('MarkItDown')
  })
})

describe('SystemStatusPanel retention enforcement', () => {
  it('shows the effective concurrency limits of this api process', () => {
    // The panel is the operator's answer to "which value actually governs
    // here" -- so the numbers must come from the runtime payload, not from
    // any hardcoded default, and the labels must name the process scope.
    const markup = renderPanel(
      runtimeFixture({
        runs: { admission_max_concurrent: 42, queue_max_size: 17 },
      }),
    )
    expect(markup).toContain('Run-Zulassungsgrenze')
    expect(markup).toContain('42')
    expect(markup).toContain('Warteschlange bis 17')
    expect(markup).toContain('Chat-Limit je API-Prozess')
    expect(markup).toContain('Stream-Leser je API-Prozess')
    expect(markup).toContain('128')
    expect(markup).toContain('Agent-Checkpointer-Pool je Prozess')
  })

  it('hides the checkpointer row against an api that does not publish it', () => {
    // Both older-api shapes: the field missing, and the BLOCK missing
    // entirely -- only the second exercises the optional chaining.
    const markup = renderPanel(
      runtimeFixture({ agents: { checkpointer_pool_size: undefined } }),
    )
    expect(markup).not.toContain('undefined')
    expect(markup).not.toContain('Agent-Checkpointer-Pool')

    const withoutBlock = { ...runtimeFixture() }
    delete withoutBlock.agents
    const blockless = renderPanel(withoutBlock)
    expect(blockless).not.toContain('undefined')
    expect(blockless).not.toContain('Agent-Checkpointer-Pool')
  })

  it('hides the concurrency rows against an older api image', () => {
    // Version skew: a pre-0.2.0.8 api does not publish these fields. The
    // declared type marks them optional; a row reading "undefined" would
    // be worse than no row.
    const markup = renderPanel(
      runtimeFixture({
        api: {
          chat_max_concurrent: undefined,
          stream_reader_workers: undefined,
        },
        runs: {
          admission_max_concurrent: undefined,
          queue_max_size: undefined,
        },
      }),
    )
    expect(markup).not.toContain('undefined')
    expect(markup).not.toContain('Run-Zulassungsgrenze')
    expect(markup).not.toContain('Chat-Limit je API-Prozess')
    expect(markup).not.toContain('Stream-Leser je API-Prozess')
  })

  it('keeps the admission row but drops the queue note when only queue_max_size is missing', () => {
    // The two fields are built independently server-side (getattr
    // fallbacks in system_runtime.py), so this combination is reachable
    // -- and it exercises the INNER guard alone, which the all-missing
    // case above never reaches.
    const markup = renderPanel(
      runtimeFixture({
        runs: { admission_max_concurrent: 42, queue_max_size: undefined },
      }),
    )
    expect(markup).toContain('Run-Zulassungsgrenze')
    expect(markup).toContain('42')
    expect(markup).not.toContain('undefined')
    expect(markup).not.toContain('Warteschlange bis')
  })

  it('flags configured retention that no worker enforces', () => {
    const markup = renderPanel(
      runtimeFixture({
        observability: {
          tracing: 'otlp',
          tracing_active: true,
          retention_enforced: false,
          retention_days: 30,
        },
      }),
    )
    expect(markup).toContain('Ohne Worker nicht durchgesetzt')
  })

  it('shows no retention warning when the worker enforces it', () => {
    const markup = renderPanel(
      runtimeFixture({
        runs: {
          execution: 'worker_dispatch',
          queue: 'valkey',
          queue_consumers: 2,
          queue_depth: 0,
          worker_dispatch: true,
        },
        observability: {
          tracing: 'otlp',
          tracing_active: true,
          retention_enforced: true,
          retention_days: 30,
        },
      }),
    )
    expect(markup).not.toContain('Ohne Worker nicht durchgesetzt')
  })
})
