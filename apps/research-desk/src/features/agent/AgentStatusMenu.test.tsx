import type { ReactNode } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'

import { TooltipProvider } from '@/components/ui/tooltip'
import { translations } from '@/i18n/translations'
import { AgentStatusMenu } from './AgentStatusMenu'
import type { AgentOverview } from './agentStatusOverview'
import { DEMO_AGENT_OVERVIEW_SOURCE, DEMO_AGENT_TIERS } from './demo'

vi.mock('@/i18n/LocaleProvider', () => ({
  useLocale: () => ({ locale: 'de', t: translations.de }),
}))

// The menu body renders into a portal only while open; inline it so the
// static markup carries the rows.
vi.mock('@/components/ui/dropdown-menu', () => {
  const Passthrough = ({ children }: { children?: ReactNode }) => <>{children}</>
  return {
    DropdownMenu: Passthrough,
    DropdownMenuContent: Passthrough,
    DropdownMenuItem: Passthrough,
    DropdownMenuSeparator: () => null,
    DropdownMenuTrigger: Passthrough,
  }
})

const overview: AgentOverview = {
  brain: 'workspace_agent',
  kernel: 'selectable',
  durable: true,
  approvals: null,
  tools: [],
  limits: DEMO_AGENT_OVERVIEW_SOURCE.limits ?? null,
}

function clarificationRounds(props: Partial<Parameters<typeof AgentStatusMenu>[0]>) {
  const html = renderToStaticMarkup(
    <TooltipProvider>
      <AgentStatusMenu
        autonomyHint=""
        autonomyLabel="Standard"
        disabled={false}
        memoryEnabled={false}
        overview={overview}
        responseForm="auto"
        responseFormValue="Automatisch"
        sourcePolicy={{ web: 'available', knowledge: 'available' }}
        {...props}
      />
    </TooltipProvider>,
  )
  const label = translations.de.agent.overview.rowClarificationRounds
  return html.match(new RegExp(`${label}</span><span[^>]*>([^<]*)</span>`))?.[1] ?? null
}

describe('agent status menu clarification rounds', () => {
  const hard = (limit: number) =>
    translations.de.agent.overview.limitHard.replace('{limit}', String(limit))

  it('shows the selected tier cap the server enforces, not the operator ceiling', () => {
    // Operator ceiling 2, gruendlich 1: before a run publishes its live
    // limit, the tier's published entry is the number that applies.
    expect(overview.limits?.mission.clarification_rounds).toBe(2)
    expect(clarificationRounds({ tier: 'gruendlich', tiers: DEMO_AGENT_TIERS })).toBe(hard(1))
    expect(clarificationRounds({ tier: 'schnell', tiers: DEMO_AGENT_TIERS })).toBe(hard(0))
  })

  it('keeps the operator ceiling without a selected tier', () => {
    expect(clarificationRounds({ tier: null, tiers: DEMO_AGENT_TIERS })).toBe(hard(2))
  })
})
