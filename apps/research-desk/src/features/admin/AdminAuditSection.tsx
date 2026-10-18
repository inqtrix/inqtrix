import { useState } from 'react'

import type { AdminAuditFilters } from '@/api/inqtrixClient'
import { AuditPanel } from './AuditPanel'
import { useAdminAudit } from './useAdminAudit'

/**
 * Self-contained admin-audit section: owns the filter state and the
 * cursor-paginated hook so SettingsWorkspace only mounts it (the panel
 * switch stays hook-free per section).
 */
export function AdminAuditSection({
  demo,
  enabled,
  traceUiConfigured,
}: {
  demo: boolean
  enabled: boolean
  traceUiConfigured: boolean
}) {
  const [filters, setFilters] = useState<AdminAuditFilters>({ limit: 50 })
  const audit = useAdminAudit({ demo, enabled, filters })
  return (
    <AuditPanel
      audit={audit}
      demo={demo}
      filters={filters}
      onFiltersChange={setFilters}
      traceUiConfigured={traceUiConfigured}
    />
  )
}
