/** Persistent source availability for one Agent Desk session. */
export type AgentSourcePolicyState = 'available' | 'disabled'

export type AgentSourcePolicy = {
  web: AgentSourcePolicyState
  knowledge: AgentSourcePolicyState
}

export type AgentExecutionLimit = {
  used: number | null
  limit: number
  ceiling: number
  recoverable: boolean
  extendable: boolean
  reason: string | null
}

export type AgentExecutionSnapshot = {
  effectiveMode: string | null
  responseForm: string | null
  depth: string | null
  model: string | null
  reasoningEffort: string | null
  sourcePolicy: AgentSourcePolicy
  /** Which knowledge boundary the run searched: the session switch (`all`),
   * attached chips (`pinned`) or the session's inherited scope; null on
   * servers that predate the field. */
  knowledgeScope: AgentKnowledgeScopeReport | null
  /** Collections inside that boundary (0 when unknown). */
  knowledgeCollectionCount: number
  consentReason: string | null
  toolUseCounts: { web: number; knowledge: number }
  /** Web searches the run's own provider calls executed; null when the
   * payload did not record it (a snapshot from before the count). */
  webSearches: number | null
  limits: Record<string, AgentExecutionLimit>
  /** Tools the user approved run-wide (`approval_scope: 'run'`);
   * empty when nothing is granted, or when an older server that does not
   * publish `tool_grants` yet sends the snapshot without the field. */
  toolGrants: string[]
}

export type AgentKnowledgeScopeReport = 'all' | 'pinned' | 'inherited'

export type AgentExecutionDisplay = {
  effectiveMode: string
  responseForm: string
  depth: string
}

export const DEFAULT_AGENT_SOURCE_POLICY: AgentSourcePolicy = {
  web: 'available',
  knowledge: 'available',
}

/**
 * Effective status values: the accepted run's canonical snapshot wins over
 * the composer's selection once a run was admitted.
 */
export function resolveAgentExecutionDisplay({
  execution,
  selectedDepth,
  selectedMode,
  selectedResponseForm,
}: {
  execution: AgentExecutionSnapshot | null
  selectedDepth: string
  selectedMode: string
  selectedResponseForm: string
}): AgentExecutionDisplay {
  if (execution) {
    return {
      effectiveMode: execution.effectiveMode ?? selectedMode,
      responseForm: execution.responseForm ?? selectedResponseForm,
      depth: execution.depth ?? selectedDepth,
    }
  }
  return {
    effectiveMode: selectedMode,
    responseForm: selectedResponseForm,
    depth: selectedDepth,
  }
}

export function normalizeAgentSourcePolicy(value: unknown): AgentSourcePolicy {
  if (!value || typeof value !== 'object') {
    return { ...DEFAULT_AGENT_SOURCE_POLICY }
  }
  const candidate = value as Record<string, unknown>
  return {
    web: candidate.web === 'disabled' ? 'disabled' : 'available',
    knowledge:
      candidate.knowledge === 'disabled' ? 'disabled' : 'available',
  }
}

/** The composer's direct commands. `/web` turns the session's web search ON
 * (a no-op when it already is — depth is the tier's job, so there is no
 * second web control); `/wissen` turns the session's knowledge switch ON:
 * every collection the user may see, until switched off; `/canvas` pins the
 * session's response form to the canvas document.
 *
 * The name says "composer", not "source": the response form is not a
 * source, and a table called SOURCE that carries it would lie about what
 * it holds. */
export type AgentComposerCommand = 'web' | 'wissen' | 'canvas'

/**
 * Converts an exact trailing composer command into its intent and strips it
 * from the question. Slashes inside URLs and prose are untouched; the
 * command must be its own final whitespace-delimited token.
 */
export function extractTrailingComposerCommand(value: string): {
  command: AgentComposerCommand
  question: string
} | null {
  const match = /(?:^|\s)\/(web|wissen|canvas)\s*$/i.exec(value)
  if (!match) return null
  const command = match[1].toLowerCase() as AgentComposerCommand
  const question = value.slice(0, match.index).trim()
  return { command, question }
}

/** What a composer command does to the session — ONE definition for the
 * palette pick and for a trailing token on send, so neither path can drift
 * back to attaching chips: `/web` switches the web search on, `/wissen`
 * switches project knowledge on (every visible collection), `/canvas` pins
 * the response form. Every command changes SESSION state, never the
 * question text and never a chip. */
export function composerCommandEffect(
  command: AgentComposerCommand,
): {
  web?: 'available'
  knowledgeSwitch?: 'all'
  responseForm?: 'canvas'
} {
  if (command === 'web') return { web: 'available' }
  if (command === 'canvas') return { responseForm: 'canvas' }
  return { knowledgeSwitch: 'all' }
}

/**
 * The session knowledge switch. `undefined` = the session never asked for
 * project knowledge (no glyph, no filters); `'all'` = every collection the
 * user may see (`/wissen` or the glyph); `'off'` = switched off again (the
 * glyph stays, struck through). An attached `@`-collection always wins: it
 * pins the scope to that collection and hides the glyph.
 */
export type AgentKnowledgeSwitch = 'all' | 'off'
export type AgentKnowledgeSwitchState = 'hidden' | 'on' | 'off' | 'unavailable'
/** What one submission asks the server for: chips, the whole visible
 * boundary, or nothing. */
export type AgentKnowledgeScope = 'pinned' | 'all' | 'off'

export function normalizeAgentKnowledgeSwitch(
  value: unknown,
): AgentKnowledgeSwitch | undefined {
  return value === 'all' || value === 'off' ? value : undefined
}

export function knowledgeSwitchState({
  available,
  knowledgeSwitch,
  pinned,
}: {
  available: boolean
  knowledgeSwitch: AgentKnowledgeSwitch | undefined
  /** A collection chip is attached — it pins the scope, so no glyph. */
  pinned: boolean
}): AgentKnowledgeSwitchState {
  if (knowledgeSwitch === undefined || pinned) return 'hidden'
  if (!available) return 'unavailable'
  return knowledgeSwitch === 'all' ? 'on' : 'off'
}

/** Chips pin; an ON switch asks for everything the user may see; anything
 * else sends no knowledge scope at all. Availability is NOT consulted
 * here: the server refuses an explicit request while its store is down
 * (503 with the reason), and that refusal is shown — a request rewritten
 * to "no knowledge" on the client would hide it. */
export function knowledgeScopeForSubmit({
  collectionIds,
  knowledgeSwitch,
}: {
  collectionIds: readonly string[]
  knowledgeSwitch: AgentKnowledgeSwitch | undefined
}): AgentKnowledgeScope {
  if (collectionIds.length > 0) return 'pinned'
  return knowledgeSwitch === 'all' ? 'all' : 'off'
}

export function knowledgePolicyForScope(
  scope: AgentKnowledgeScope,
): AgentSourcePolicyState {
  return scope === 'off' ? 'disabled' : 'available'
}

export function toolUseCountsFromSnapshot(snapshot: unknown): {
  web: number
  knowledge: number
} | null {
  return normalizeAgentExecutionSnapshot(snapshot)?.toolUseCounts ?? null
}

/** Canonical adapter for the server-published execution block. All status
 * surfaces consume this one normalized value rather than reading snapshot
 * keys independently. */
export function normalizeAgentExecutionSnapshot(
  snapshot: unknown,
): AgentExecutionSnapshot | null {
  if (!snapshot || typeof snapshot !== 'object') return null
  const execution = (snapshot as Record<string, unknown>).execution
  if (!execution || typeof execution !== 'object') return null
  const record = execution as Record<string, unknown>
  const rawCounts = record.tool_use_counts
  const counts = rawCounts && typeof rawCounts === 'object'
    ? rawCounts as Record<string, unknown>
    : {}
  const stringOrNull = (value: unknown): string | null =>
    typeof value === 'string' && value.length > 0 ? value : null
  const rawLimits = record.limits && typeof record.limits === 'object'
    ? record.limits as Record<string, unknown>
    : {}
  const limits = Object.fromEntries(
    Object.entries(rawLimits).flatMap(([key, value]) => {
      if (!value || typeof value !== 'object') return []
      const limitRecord = value as Record<string, unknown>
      const limit = nonNegativeInteger(limitRecord.limit)
      const ceiling = nonNegativeInteger(limitRecord.ceiling)
      const recoverable = limitRecord.recoverable
      const extendable = limitRecord.extendable
      if (
        limit === null
        || ceiling === null
        || ceiling < limit
        || typeof recoverable !== 'boolean'
        || typeof extendable !== 'boolean'
        || (extendable && (!recoverable || ceiling <= limit))
      ) return []
      return [[key, {
        used: nonNegativeInteger(limitRecord.used),
        limit,
        ceiling,
        recoverable,
        extendable,
        reason: stringOrNull(limitRecord.reason),
      } satisfies AgentExecutionLimit]]
    }),
  )
  return {
    effectiveMode: stringOrNull(record.effective_mode ?? record.mode),
    responseForm: stringOrNull(record.response_form),
    depth: stringOrNull(record.depth),
    model: stringOrNull(record.model),
    reasoningEffort: stringOrNull(record.reasoning_effort),
    sourcePolicy: normalizeAgentSourcePolicy(record.source_policy),
    knowledgeScope:
      record.knowledge_scope === 'all'
      || record.knowledge_scope === 'pinned'
      || record.knowledge_scope === 'inherited'
        ? record.knowledge_scope
        : null,
    knowledgeCollectionCount:
      nonNegativeInteger(record.knowledge_collection_count) ?? 0,
    consentReason: stringOrNull(record.consent_reason ?? record.consent),
    webSearches: nonNegativeInteger(record.web_searches) ?? null,
    toolUseCounts: {
      web: typeof counts.web === 'number' && counts.web >= 0 ? counts.web : 0,
      knowledge:
        typeof counts.knowledge === 'number' && counts.knowledge >= 0
          ? counts.knowledge
          : 0,
    },
    limits,
    toolGrants: Array.isArray(record.tool_grants)
      ? record.tool_grants.filter(
        (value): value is string => typeof value === 'string' && value.length > 0,
      )
      : [],
  }
}

function nonNegativeInteger(value: unknown): number | null {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0
    ? value
    : null
}

/** The model a session was last run with, mirroring the composer's exclusivity
 * contract: a tier pick clears the explicit model, a model pick clears the
 * tier. Stored in `items_json` beside the source policy. */
export type AgentSessionModelSelection = {
  model: string | null
  tier: 'high' | 'mid' | 'fast' | null
  effort: string | null
}

const AGENT_MODEL_TIERS = new Set(['high', 'mid', 'fast'])

/** Parse a stored selection defensively: an unknown tier resolves to null so a
 * row written by a future build cannot pin a tier this one does not know.
 * Returns null when nothing was ever picked, which is what lets the account
 * preference seed the composer instead. */
export function normalizeAgentModelSelection(
  value: unknown,
): AgentSessionModelSelection | null {
  if (!value || typeof value !== 'object') return null
  const candidate = value as Record<string, unknown>
  const model = typeof candidate.model === 'string' && candidate.model ? candidate.model : null
  const rawTier = typeof candidate.tier === 'string' ? candidate.tier : ''
  const tier = AGENT_MODEL_TIERS.has(rawTier)
    ? (rawTier as 'high' | 'mid' | 'fast')
    : null
  const effort = typeof candidate.effort === 'string' && candidate.effort
    ? candidate.effort
    : null
  if (model === null && tier === null && effort === null) return null
  // Exclusivity: an explicit model wins, exactly as the reducer enforces.
  return model !== null ? { model, tier: null, effort } : { model: null, tier, effort }
}

/** Stable key for change detection. Must cover every field, or a changed
 * selection never reaches the server. */
export function agentModelSelectionKey(
  selection: AgentSessionModelSelection | null | undefined,
): string {
  if (!selection) return '-'
  return `${selection.model ?? ''}|${selection.tier ?? ''}|${selection.effort ?? ''}`
}

/** The web switch's visible state: `on` (brand chip), `off` (struck-through
 * glyph) or `unavailable` (server without web search, or a disabled
 * composer). Exactly these three, so on and off are never told apart by a
 * hover text alone. */
export type AgentWebSwitchState = 'on' | 'off' | 'unavailable'

export function webSwitchState({
  available,
  policy,
}: {
  available: boolean
  policy: AgentSourcePolicyState
}): AgentWebSwitchState {
  // A composer lock (run in flight, surface transition) is not a source
  // state: the glyph keeps showing on/off and merely stops reacting.
  if (!available) return 'unavailable'
  return policy === 'available' ? 'on' : 'off'
}
