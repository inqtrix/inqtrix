import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import type { AdminAuditEvent } from '@/api/inqtrixClient'
import { LocaleProvider } from '@/i18n/LocaleProvider'
import { AuditPanel } from './AuditPanel'
import type { useAdminAudit } from './useAdminAudit'

describe('AuditPanel session resources', () => {
  it('renders the durable session reference without a credential fallback', () => {
    const safeReference = 'ses_0123456789abcdef'
    const formerCredential = 'x'.repeat(43)
    const event: AdminAuditEvent = {
      id: 1,
      occurred_at: 1_774_800_000,
      action: 'auth.logout',
      resource_type: 'session',
      resource_id: safeReference,
      actor_pseudonym: 'usr_0123456789abcdef',
      actor_type: 'user',
      outcome: 'success',
      workspace_id: null,
      detail: {},
      origin: { auth_method: 'local' },
      correlation: { request_id: 'req-1' },
    }
    const audit: ReturnType<typeof useAdminAudit> = {
      available: true,
      demo: false,
      events: [event],
      nextCursor: null,
      status: 'ready',
      error: null,
      reload: () => undefined,
      loadMore: () => undefined,
    }

    const markup = renderToStaticMarkup(
      <LocaleProvider>
        <AuditPanel
          audit={audit}
          demo={false}
          filters={{ limit: 50 }}
          onFiltersChange={() => undefined}
          traceUiConfigured={false}
        />
      </LocaleProvider>,
    )

    expect(markup).toContain(`session · ${safeReference}`)
    expect(markup).not.toContain(formerCredential)
  })
})
