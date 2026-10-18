import { createDefaultFileLibrarySections } from '@/features/files/sections'
import { seedKnowledgeThreadItem } from '@/features/knowledge/demo'
import { DEMO_SHARED_IN_RUN_ID, resetDemoShares } from '@/features/sharing/demoShares'
import { parseChatRule, parseChatThread, parseResearchRun } from './markdown'
import { EMPTY_CANVAS_STATE } from '@/features/canvas/types'
import { createBootstrapKnowledgeSession } from './knowledgeSessionDefaults'
import { DEFAULT_PANEL_LAYOUT } from './panelLayout'
import { defaultExplorerSortState } from './explorerSort'
import type {
  EditorCommentThreadRecord,
  EditorDocumentRecord,
  EditorFolderRecord,
  EditorUiState,
  ChatRuleRecord,
  ChatThreadGroupRecord,
  ChatThreadRecord,
  FileAssetRecord,
  FileGroupRecord,
  FileLibrarySectionRecord,
  KnowledgeSessionGroupRecord,
  KnowledgeSessionRecord,
  PinnedExplorerState,
  ProjectState,
  ResearchRunRecord,
  VectorIndexRecord,
} from './types'
import { PROJECT_SCHEMA_VERSION } from './types'
import { getOrCreateBrowserWorkspaceId } from './workspaceId'

// Neutral fixed instant for all STORED demo data so no real creation date can
// be inferred (anonymisation). The live/queued runs below are the deliberate
// exception — they are a "happening now" simulation, so they use the current
// session time to keep a believable runtime.
const seedCreatedAt = '2026-01-01T00:00:00.000Z'

const SEED_SECTION_LEGAL = 'file-section-legal'
const SEED_SECTION_MARKET = 'file-section-market'
const SEED_SECTION_OWN = 'file-section-own'
const SEED_GROUP_EU_AI_ACT = 'file-group-eu-ai-act'
const SEED_GROUP_PRIVACY = 'file-group-privacy-rights'
const SEED_GROUP_STANDARDS = 'file-group-standards-security'
const SEED_GROUP_CASE_LAW = 'file-group-case-law'
const SEED_GROUP_SUPERVISION = 'file-group-supervision'

const DEMO_CHAT_GROUP = 'chat-group-demos'
const DEMO_KNOWLEDGE_SESSION_ID = 'knowledge-session-demo'
/** The run whose report opens in the Editor (a real exported report). */
const DEMO_EDITOR_RUN_ID = 'run_1c6d7daf4c9f45a5b023693627860b61'
/** The hand-built live run id — shared with the demo live-progress simulator
 * in ResearchDesk so the two can never drift. */
export const DEMO_RUNNING_RUN_ID = 'RO-live-9001'
/** Rounds the live run advertises; the simulator counts within this bound. */
export const DEMO_RUNNING_MAX_ROUNDS = 4

// Real demo content: the bundled Inqtrix project export, run through the SAME
// import parsers the directory loader uses (one parse path, no second seed
// format). Vite inlines these markdown files at build time.
const demoRunFiles = import.meta.glob('./demoContent/search-history/*.md', {
  eager: true,
  import: 'default',
  query: '?raw',
}) as Record<string, string>
const demoChatFiles = import.meta.glob('./demoContent/chat-history/*.md', {
  eager: true,
  import: 'default',
  query: '?raw',
}) as Record<string, string>
const demoRuleFiles = import.meta.glob('./demoContent/rules/*.md', {
  eager: true,
  import: 'default',
  query: '?raw',
}) as Record<string, string>

/** Parsed real research runs, newest first. One run is marked shared-in so
 * the demo also shows the recipient "Mit mir geteilt" group. */
function parsedDemoRuns(): ResearchRunRecord[] {
  return Object.values(demoRunFiles)
    .map((markdown) => parseResearchRun(markdown))
    .map((run) =>
      run.runId === DEMO_SHARED_IN_RUN_ID
        ? { ...run, access: { mode: 'shared' as const, permission: 'view' as const } }
        : run,
    )
    .sort((a, b) => b.submittedAt.localeCompare(a.submittedAt))
}

/** Parsed real chat threads, newest first. */
function parsedDemoChats(): ChatThreadRecord[] {
  return Object.values(demoChatFiles)
    .map((markdown) => parseChatThread(markdown))
    .sort((a, b) => b.updatedAt.localeCompare(a.updatedAt))
}

/** Parsed real prompt templates (rules), stable by title. */
function parsedDemoRules(): ChatRuleRecord[] {
  return Object.values(demoRuleFiles)
    .map((markdown) => parseChatRule(markdown))
    .sort((a, b) => a.title.localeCompare(b.title))
}

const emptyEditorUi: EditorUiState = {
  activeDocumentId: null,
  assistantDraft: '',
  isAssistantVisible: true,
  isCommentPanelVisible: true,
  isDiffVisible: false,
  isTreeVisible: true,
  openDocumentIds: [],
  panelTab: 'comments',
  selectedCommentId: null,
  viewMode: 'live',
}

const emptyPinnedExplorer: PinnedExplorerState = {
  chatThreadIds: [],
  editorDocumentIds: [],
  knowledgeSessionIds: [],
  agentSessionIds: [],
}

function createKnowledgeSession(
  id: string,
  title: string,
  createdAt: string,
): KnowledgeSessionRecord {
  return {
    createdAt,
    id,
    title,
    updatedAt: createdAt,
  }
}

function createKnowledgeSessionGroup(
  id: string,
  title: string,
  createdAt: string,
): KnowledgeSessionGroupRecord {
  return {
    createdAt,
    id,
    title,
    updatedAt: createdAt,
  }
}

export function createEmptyProjectState(): ProjectState {
  const now = new Date().toISOString()
  const defaultSections = createDefaultFileLibrarySections(now)
  const defaultKnowledgeSession = createBootstrapKnowledgeSession(now)

  return {
    chatRuleOrder: [],
    chatRules: {},
    chatThreadGroupMemberships: {},
    chatThreadGroupOrder: [],
    chatThreadGroups: {},
    chatThreadOrder: [],
    chatThreads: {},
    connection: {
      kind: 'local',
      writable: false,
    },
    dirty: false,
    editorComments: {},
    editorDocumentOrder: [],
    editorDocuments: {},
    editorFolderOrder: [],
    editorFolders: {},
    editorSuggestionGroups: {},
    editorSuggestions: {},
    editorUi: emptyEditorUi,
    fileAssetOrder: [],
    fileAssets: {},
    fileGroupOrder: [],
    fileGroups: {},
    fileLibrarySectionOrder: defaultSections.map((section) => section.id),
    fileLibrarySections: Object.fromEntries(defaultSections.map((section) => [section.id, section])),
    indexingJobs: {},
    knowledgeItemOrder: [],
    knowledgeItems: {},
    knowledgeSessionGroupMemberships: {},
    knowledgeSessionGroupOrder: [],
    knowledgeSessionGroups: {},
    knowledgeSessionOrder: [defaultKnowledgeSession.id],
    knowledgeSessions: { [defaultKnowledgeSession.id]: defaultKnowledgeSession },
    selectedKnowledgeSessionId: defaultKnowledgeSession.id,
    agentRuns: {},
    agentSessionArtifacts: {},
    agentSessionGroupOrder: [],
    agentSessionGroups: {},
    agentSessionOrder: [],
    agentSessions: {},
    selectedAgentSessionId: null,
    agentCanvas: EMPTY_CANVAS_STATE,
    agentPlanDrafts: {},
    localRunCounter: 1,
    preferences: {
      agentMemoryEnabled: false,
      agentModelTier: '',
      chatModelTier: '',
      contrastMode: 'standard',
      locale: 'de',
      theme: 'system',
      themePreset: 'petrol',
      userBubbleTone: 'gray',
    },
    project: {
      createdAt: now,
      name: 'Untitled Inqtrix Project',
      schemaVersion: PROJECT_SCHEMA_VERSION,
      updatedAt: now,
    },
    researchRunOrder: [],
    researchRuns: {},
    serverSyncEnabled: false,
    projectEpoch: 0,
    vectorIndexOrder: [],
    vectorIndexes: {},
    workspaceId: getOrCreateBrowserWorkspaceId(),
    ui: {
      activeFilter: 'all',
      activeView: 'research',
      chatChainingEnabled: false,
      expandedJobId: null,
      isAgentSessionsVisible: true,
      isChatHistoryVisible: true,
      isKnowledgeHistoryVisible: true,
      isComposerVisible: true,
      isReportExpanded: false,
      isReportVisible: true,
      panelLayout: DEFAULT_PANEL_LAYOUT,
      pendingChatAttachmentRefs: [],
      pendingChatReportRunId: null,
      explorerSort: defaultExplorerSortState(),
      pinnedExplorer: emptyPinnedExplorer,
      selectedAgentEffort: null,
      selectedAgentModel: null,
      selectedAgentModelTier: null,
      selectedChatModel: null,
      selectedChatEffort: null,
      selectedChatModelTier: null,
      selectedChatThreadId: null,
      selectedAgentSessionId: null,
      selectedJobId: null,
      selectedStack: 'anthropic_perplexity',
    },
  }
}

export function createSeedProjectState(): ProjectState {
  // A fresh demo starts from clean seeded shares (rebuild-on-toggle invariant).
  resetDemoShares()
  const researchRuns = seedResearchRuns()
  const researchRunOrder = researchRuns.map((run) => run.runId)
  const chatThreads = seedChatThreads()
  const chatThreadOrder = chatThreads.map((thread) => thread.id)
  const chatThreadGroups = seedChatThreadGroups()
  const chatThreadGroupOrder = chatThreadGroups.map((group) => group.id)
  const chatRules = seedChatRules()
  const chatRuleOrder = chatRules.map((rule) => rule.id)
  const editorFolders = seedEditorFolders()
  const editorFolderOrder = editorFolders.map((folder) => folder.id)
  const editorDocuments = seedEditorDocuments(researchRuns)
  const editorDocumentOrder = editorDocuments.map((document) => document.id)
  const editorComments = seedEditorComments()
  const openDocumentIds = editorDocumentOrder.slice(0, 2)
  const fileLibrarySections = seedFileLibrarySections()
  const fileGroups = seedFileGroups()
  const fileAssets = seedFileAssets()
  const vectorIndexes = seedVectorIndexes()
  const knowledgeSeedItem = seedKnowledgeThreadItem(seedCreatedAt)
  const knowledgeSession = createKnowledgeSession(
    DEMO_KNOWLEDGE_SESSION_ID,
    'AI Act Hochrisiko',
    seedCreatedAt,
  )
  const knowledgeSessionGroup = createKnowledgeSessionGroup(
    'knowledge-session-group-demo-eu-law',
    'EU-Recht',
    seedCreatedAt,
  )
  const knowledgeSeedItemInSession = {
    ...knowledgeSeedItem,
    sessionId: knowledgeSession.id,
  }
  // Open on a real completed report (not the live/queued run) so the report
  // panel shows real content immediately.
  const firstReportRunId =
    researchRuns.find((run) => run.status === 'completed' && run.result?.markdown)
      ?.runId
    ?? researchRunOrder[0]
    ?? null

  return {
    chatRuleOrder,
    chatRules: Object.fromEntries(chatRules.map((rule) => [rule.id, rule])),
    chatThreadGroupMemberships: Object.fromEntries(
      chatThreads.map((thread) => [thread.id, DEMO_CHAT_GROUP]),
    ),
    chatThreadGroupOrder,
    chatThreadGroups: Object.fromEntries(chatThreadGroups.map((group) => [group.id, group])),
    chatThreadOrder,
    chatThreads: Object.fromEntries(chatThreads.map((thread) => [thread.id, thread])),
    connection: {
      kind: 'demo',
      writable: false,
    },
    dirty: false,
    editorComments: Object.fromEntries(editorComments.map((comment) => [comment.id, comment])),
    editorDocumentOrder,
    editorDocuments: Object.fromEntries(editorDocuments.map((document) => [document.id, document])),
    editorFolderOrder,
    editorFolders: Object.fromEntries(editorFolders.map((folder) => [folder.id, folder])),
    editorSuggestionGroups: {},
    editorSuggestions: {},
    editorUi: {
      ...emptyEditorUi,
      activeDocumentId: openDocumentIds[0] ?? null,
      openDocumentIds,
    },
    fileAssetOrder: fileAssets.map((asset) => asset.id),
    fileAssets: Object.fromEntries(fileAssets.map((asset) => [asset.id, asset])),
    fileGroupOrder: fileGroups.map((group) => group.id),
    fileGroups: Object.fromEntries(fileGroups.map((group) => [group.id, group])),
    fileLibrarySectionOrder: fileLibrarySections.map((section) => section.id),
    fileLibrarySections: Object.fromEntries(fileLibrarySections.map((section) => [section.id, section])),
    indexingJobs: {
      'vector-index-eu-recht': {
        completedDocuments: 0,
        currentDocumentTitle: 'Rechtsgutachten KI-Haftung',
        jobId: 'demo-seed-eu-recht',
        percent: 0,
        // Incremental add: only the new (pending) rechtsgutachten runs; the
        // three embedded members keep reading "Indexiert" (see seedVectorIndexes).
        runningFileIds: ['file-asset-rechtsgutachten'],
        source: 'demo',
        startedAt: new Date(Date.now() - 4_000).toISOString(),
        totalDocuments: 1,
      },
      // A second reindex waiting behind it — shows the "In Warteschlange"
      // state on load; the simulator promotes it on its first tick. A full
      // refresh, so its whole member set is the working set.
      'vector-index-anbieter': {
        completedDocuments: 0,
        jobId: 'demo-seed-anbieter',
        percent: 0,
        queuePosition: 1,
        runningFileIds: ['file-asset-perplexity-db', 'file-asset-azure-foundry', 'file-asset-bsi-kriterien'],
        source: 'demo',
        startedAt: new Date(Date.now() - 1_000).toISOString(),
        totalDocuments: 3,
      },
    },
    knowledgeItemOrder: [knowledgeSeedItemInSession.id],
    knowledgeItems: { [knowledgeSeedItemInSession.id]: knowledgeSeedItemInSession },
    knowledgeSessionGroupMemberships: { [knowledgeSession.id]: knowledgeSessionGroup.id },
    knowledgeSessionGroupOrder: [knowledgeSessionGroup.id],
    knowledgeSessionGroups: { [knowledgeSessionGroup.id]: knowledgeSessionGroup },
    knowledgeSessionOrder: [knowledgeSession.id],
    knowledgeSessions: { [knowledgeSession.id]: knowledgeSession },
    selectedKnowledgeSessionId: knowledgeSession.id,
    agentRuns: {},
    agentSessionArtifacts: {},
    agentSessionGroupOrder: [],
    agentSessionGroups: {},
    agentSessionOrder: [],
    agentSessions: {},
    selectedAgentSessionId: null,
    agentCanvas: EMPTY_CANVAS_STATE,
    agentPlanDrafts: {},
    localRunCounter: 248,
    preferences: {
      agentMemoryEnabled: false,
      agentModelTier: '',
      chatModelTier: '',
      contrastMode: 'standard',
      locale: 'de',
      theme: 'dark',
      themePreset: 'petrol',
      userBubbleTone: 'gray',
    },
    project: {
      createdAt: seedCreatedAt,
      name: 'Inqtrix Research Desk Demo',
      schemaVersion: PROJECT_SCHEMA_VERSION,
      updatedAt: seedCreatedAt,
    },
    researchRunOrder,
    researchRuns: Object.fromEntries(researchRuns.map((run) => [run.runId, run])),
    // The demo project showcases the server-synced state visually; the
    // !isDemoMode capability gate still blocks any real server call, so
    // this only lights up the "Synced" badge in the chat history.
    serverSyncEnabled: true,
    projectEpoch: 0,
    vectorIndexOrder: vectorIndexes.map((index) => index.id),
    vectorIndexes: Object.fromEntries(vectorIndexes.map((index) => [index.id, index])),
    workspaceId: 'ws_demo_research_desk',
    ui: {
      activeFilter: 'all',
      activeView: 'research',
      chatChainingEnabled: false,
      expandedJobId: firstReportRunId,
      isAgentSessionsVisible: true,
      isChatHistoryVisible: true,
      isKnowledgeHistoryVisible: true,
      isComposerVisible: true,
      isReportExpanded: false,
      isReportVisible: true,
      panelLayout: DEFAULT_PANEL_LAYOUT,
      pendingChatAttachmentRefs: [],
      pendingChatReportRunId: null,
      explorerSort: defaultExplorerSortState(),
      pinnedExplorer: {
        chatThreadIds: chatThreadOrder.slice(0, 1),
        editorDocumentIds: editorDocumentOrder.slice(0, 1),
        knowledgeSessionIds: [],
        agentSessionIds: [],
      },
      selectedAgentEffort: null,
      selectedAgentModel: null,
      selectedAgentModelTier: null,
      selectedChatModel: null,
      selectedChatEffort: null,
      selectedChatModelTier: null,
      selectedChatThreadId: chatThreadOrder[0] ?? null,
      selectedAgentSessionId: null,
      selectedJobId: firstReportRunId,
      selectedStack: 'anthropic_perplexity',
    },
  }
}

function seedChatThreadGroups(): ChatThreadGroupRecord[] {
  return [
    {
      createdAt: seedCreatedAt,
      id: DEMO_CHAT_GROUP,
      title: 'Demos',
      updatedAt: seedCreatedAt,
    },
  ]
}

function seedChatRules(): ChatRuleRecord[] {
  return parsedDemoRules()
}

function seedFileLibrarySections(): FileLibrarySectionRecord[] {
  // Keep the canonical temporary section (chat/editor uploads target it via
  // section with `kind: 'temporary'`); it stays empty in the demo and the rail hides it
  // until it has documents. The three custom collections mirror the database
  // design screenshots.
  const temporary = createDefaultFileLibrarySections(seedCreatedAt).filter((section) => section.kind === 'temporary')
  return [
    ...temporary,
    { createdAt: seedCreatedAt, id: SEED_SECTION_LEGAL, kind: 'custom', title: 'Rechtliche Grundlagen', updatedAt: seedCreatedAt },
    { createdAt: seedCreatedAt, id: SEED_SECTION_MARKET, kind: 'custom', title: 'Anbieter & Markt', updatedAt: seedCreatedAt },
    { createdAt: seedCreatedAt, id: SEED_SECTION_OWN, kind: 'custom', title: 'Eigene Dokumente', updatedAt: seedCreatedAt },
  ]
}

function seedFileGroups(): FileGroupRecord[] {
  return [
    { createdAt: seedCreatedAt, id: SEED_GROUP_EU_AI_ACT, sectionId: SEED_SECTION_LEGAL, title: 'EU AI Act', updatedAt: seedCreatedAt },
    { createdAt: seedCreatedAt, id: SEED_GROUP_PRIVACY, sectionId: SEED_SECTION_LEGAL, title: 'Datenschutz & Grundrechte', updatedAt: seedCreatedAt },
    { createdAt: seedCreatedAt, id: SEED_GROUP_STANDARDS, sectionId: SEED_SECTION_LEGAL, title: 'Standards & Sicherheit', updatedAt: seedCreatedAt },
    { createdAt: seedCreatedAt, id: SEED_GROUP_CASE_LAW, sectionId: SEED_SECTION_LEGAL, title: 'Rechtsprechung', updatedAt: seedCreatedAt },
    { createdAt: seedCreatedAt, id: SEED_GROUP_SUPERVISION, sectionId: SEED_SECTION_LEGAL, title: 'Aufsicht & Umsetzung', updatedAt: seedCreatedAt },
  ]
}

function seedFileAsset(
  overrides: Partial<FileAssetRecord> & Pick<FileAssetRecord, 'fileName' | 'id' | 'label' | 'mimeType' | 'sectionId' | 'sizeBytes' | 'title'>,
): FileAssetRecord {
  return {
    createdAt: seedCreatedAt,
    extractedText: `${overrides.title} — Demo-Auszug für die Datenbank-Vorführung.`,
    groupId: null,
    origin: 'library',
    pageCount: null,
    parseStatus: 'parsed',
    parseWarning: null,
    textTruncated: false,
    updatedAt: seedCreatedAt,
    ...overrides,
  }
}

const MIME_PDF = 'application/pdf'
const MIME_DOCX = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
const MIME_XLSX = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
const MIME_TXT = 'text/plain'

type LegalDemoDocument = {
  fileName: string
  groupId: string | null
  label: string
  pageCount: number
  sizeBytes: number
}

const LEGAL_DEMO_DOCUMENTS: LegalDemoDocument[] = [
  { fileName: 'AI-Act-Umsetzungsleitfaden.pdf', groupId: SEED_GROUP_EU_AI_ACT, label: 'ai-act-umsetzung', pageCount: 31, sizeBytes: 714752 },
  { fileName: 'AI-Act-Verbotene-Praktiken.pdf', groupId: SEED_GROUP_EU_AI_ACT, label: 'ai-act-verbote', pageCount: 18, sizeBytes: 428032 },
  { fileName: 'AI-Act-Hochrisiko-Checkliste.pdf', groupId: SEED_GROUP_EU_AI_ACT, label: 'ai-act-hochrisiko', pageCount: 26, sizeBytes: 603136 },
  { fileName: 'AI-Act-Konformitätsbewertung.pdf', groupId: SEED_GROUP_EU_AI_ACT, label: 'konformitaetsbewertung', pageCount: 22, sizeBytes: 557056 },
  { fileName: 'AI-Act-Transparenzpflichten.pdf', groupId: SEED_GROUP_EU_AI_ACT, label: 'transparenzpflichten', pageCount: 15, sizeBytes: 331776 },
  { fileName: 'GPAI-Code-of-Practice.pdf', groupId: SEED_GROUP_EU_AI_ACT, label: 'gpai-code-practice', pageCount: 39, sizeBytes: 921600 },
  { fileName: 'Systemic-Risk-Modelle.pdf', groupId: SEED_GROUP_EU_AI_ACT, label: 'systemic-risk-modelle', pageCount: 24, sizeBytes: 577536 },
  { fileName: 'Provider-Deployer-Pflichten.pdf', groupId: SEED_GROUP_EU_AI_ACT, label: 'provider-deployer', pageCount: 21, sizeBytes: 512000 },
  { fileName: 'Technische-Dokumentation-Template.pdf', groupId: SEED_GROUP_EU_AI_ACT, label: 'tech-doku-template', pageCount: 28, sizeBytes: 696320 },
  { fileName: 'DSGVO-DPIA-KI-Systeme.pdf', groupId: SEED_GROUP_PRIVACY, label: 'dsgvo-dpia-ki', pageCount: 19, sizeBytes: 460800 },
  { fileName: 'Besondere-Kategorien-Training.pdf', groupId: SEED_GROUP_PRIVACY, label: 'besondere-kategorien', pageCount: 14, sizeBytes: 320512 },
  { fileName: 'EDPB-AI-Modelle-Notizen.pdf', groupId: SEED_GROUP_PRIVACY, label: 'edpb-ai-modelle', pageCount: 27, sizeBytes: 634880 },
  { fileName: 'Automatisierte-Entscheidungen-Art-22.pdf', groupId: SEED_GROUP_PRIVACY, label: 'automatisierte-entscheidungen', pageCount: 16, sizeBytes: 376832 },
  { fileName: 'Biometrische-Daten-Guidance.pdf', groupId: SEED_GROUP_PRIVACY, label: 'biometrische-daten', pageCount: 23, sizeBytes: 548864 },
  { fileName: 'Fundamental-Rights-Impact-Assessment.pdf', groupId: SEED_GROUP_PRIVACY, label: 'fria-assessment', pageCount: 34, sizeBytes: 819200 },
  { fileName: 'Datenminimierung-KI.pdf', groupId: SEED_GROUP_PRIVACY, label: 'datenminimierung-ki', pageCount: 11, sizeBytes: 245760 },
  { fileName: 'Einwilligung-Beschäftigtendaten.pdf', groupId: SEED_GROUP_PRIVACY, label: 'einwilligung-beschaeftigte', pageCount: 20, sizeBytes: 466944 },
  { fileName: 'ISO-42001-AI-Management.pdf', groupId: SEED_GROUP_STANDARDS, label: 'iso-42001', pageCount: 30, sizeBytes: 704512 },
  { fileName: 'ISO-23894-Risikomanagement.pdf', groupId: SEED_GROUP_STANDARDS, label: 'iso-23894', pageCount: 25, sizeBytes: 593920 },
  { fileName: 'NIST-AI-RMF-Crosswalk.pdf', groupId: SEED_GROUP_STANDARDS, label: 'nist-ai-rmf', pageCount: 36, sizeBytes: 860160 },
  { fileName: 'ENISA-AI-Cybersecurity.pdf', groupId: SEED_GROUP_STANDARDS, label: 'enisa-ai-security', pageCount: 29, sizeBytes: 688128 },
  { fileName: 'Security-Logging-Standard.pdf', groupId: SEED_GROUP_STANDARDS, label: 'security-logging', pageCount: 12, sizeBytes: 282624 },
  { fileName: 'Red-Teaming-Protokoll.pdf', groupId: SEED_GROUP_STANDARDS, label: 'red-teaming-protokoll', pageCount: 18, sizeBytes: 421888 },
  { fileName: 'Datenqualität-Kontrollen.pdf', groupId: SEED_GROUP_STANDARDS, label: 'datenqualitaet-kontrollen', pageCount: 15, sizeBytes: 352256 },
  { fileName: 'Audit-Trail-Anforderungen.pdf', groupId: SEED_GROUP_STANDARDS, label: 'audit-trail', pageCount: 10, sizeBytes: 235520 },
  { fileName: 'EuGH-SCHUFA-Scoring.pdf', groupId: SEED_GROUP_CASE_LAW, label: 'eugh-schufa-scoring', pageCount: 18, sizeBytes: 430080 },
  { fileName: 'EuGH-DSGVO-Schadensersatz.pdf', groupId: SEED_GROUP_CASE_LAW, label: 'eugh-dsgvo-schaden', pageCount: 16, sizeBytes: 393216 },
  { fileName: 'BGH-KI-Haftung-Notizen.pdf', groupId: SEED_GROUP_CASE_LAW, label: 'bgh-ki-haftung', pageCount: 12, sizeBytes: 274432 },
  { fileName: 'EuG-Plattformregulierung.pdf', groupId: SEED_GROUP_CASE_LAW, label: 'eug-plattformregulierung', pageCount: 21, sizeBytes: 503808 },
  { fileName: 'Biometrie-Zugangskontrolle-Fallakte.pdf', groupId: SEED_GROUP_CASE_LAW, label: 'biometrie-fallakte', pageCount: 14, sizeBytes: 327680 },
  { fileName: 'Employment-Screening-Case-Law.pdf', groupId: SEED_GROUP_CASE_LAW, label: 'employment-screening', pageCount: 17, sizeBytes: 401408 },
  { fileName: 'Credit-Scoring-Rechtsprechung.pdf', groupId: SEED_GROUP_CASE_LAW, label: 'credit-scoring-cases', pageCount: 19, sizeBytes: 450560 },
  { fileName: 'Public-Sector-AI-Case-Notes.pdf', groupId: SEED_GROUP_CASE_LAW, label: 'public-sector-cases', pageCount: 13, sizeBytes: 309248 },
  { fileName: 'Software-Produkthaftung-KI.pdf', groupId: SEED_GROUP_CASE_LAW, label: 'software-produkthaftung', pageCount: 20, sizeBytes: 475136 },
  { fileName: 'Commission-Implementation-Timeline.pdf', groupId: SEED_GROUP_SUPERVISION, label: 'implementation-timeline', pageCount: 11, sizeBytes: 253952 },
  { fileName: 'Nationale-Aufsichtsbehörden.pdf', groupId: SEED_GROUP_SUPERVISION, label: 'aufsichtsbehoerden', pageCount: 13, sizeBytes: 296960 },
  { fileName: 'Sanktions-und-Bußgeldrahmen.pdf', groupId: SEED_GROUP_SUPERVISION, label: 'sanktionsrahmen', pageCount: 16, sizeBytes: 376832 },
  { fileName: 'KI-Reallabore-Guidelines.pdf', groupId: SEED_GROUP_SUPERVISION, label: 'ki-reallabore', pageCount: 22, sizeBytes: 520192 },
  { fileName: 'Incident-Reporting-Template.pdf', groupId: SEED_GROUP_SUPERVISION, label: 'incident-reporting', pageCount: 9, sizeBytes: 217088 },
  { fileName: 'Post-Market-Monitoring.pdf', groupId: SEED_GROUP_SUPERVISION, label: 'post-market-monitoring', pageCount: 18, sizeBytes: 421888 },
  { fileName: 'KI-Beschaffung-Vertragsklauseln.pdf', groupId: null, label: 'ki-beschaffung', pageCount: 12, sizeBytes: 286720 },
  { fileName: 'Open-Source-Modelle-Briefing.pdf', groupId: null, label: 'open-source-modelle', pageCount: 14, sizeBytes: 335872 },
  { fileName: 'Public-Sector-AI-Use.pdf', groupId: null, label: 'public-sector-ai', pageCount: 10, sizeBytes: 231424 },
  { fileName: 'AI-Supply-Chain-Agreements.pdf', groupId: null, label: 'ai-supply-chain', pageCount: 17, sizeBytes: 397312 },
  { fileName: 'Data-Retention-Policy-AI.pdf', groupId: null, label: 'data-retention-ai', pageCount: 9, sizeBytes: 212992 },
  { fileName: 'Documentation-Gap-Analysis.pdf', groupId: null, label: 'documentation-gap', pageCount: 11, sizeBytes: 258048 },
]

function seedLegalDemoFileAssets(): FileAssetRecord[] {
  return LEGAL_DEMO_DOCUMENTS.map((document) =>
    seedFileAsset({
      fileName: document.fileName,
      groupId: document.groupId,
      id: `file-asset-${document.label}`,
      label: document.label,
      mimeType: MIME_PDF,
      pageCount: document.pageCount,
      sectionId: SEED_SECTION_LEGAL,
      sizeBytes: document.sizeBytes,
      title: document.fileName,
    }),
  )
}

function seedFileAssets(): FileAssetRecord[] {
  return [
    seedFileAsset({
      fileName: 'BSI-Kriterienkatalog-KI.pdf',
      groupId: SEED_GROUP_STANDARDS,
      id: 'file-asset-bsi-kriterien',
      label: 'bsi-kriterien',
      mimeType: MIME_PDF,
      pageCount: 58,
      sectionId: SEED_SECTION_LEGAL,
      sizeBytes: 1153434,
      title: 'BSI-Kriterienkatalog-KI.pdf',
    }),
    seedFileAsset({
      fileName: 'DSGVO-Auszug-Art-22.pdf',
      groupId: SEED_GROUP_PRIVACY,
      id: 'file-asset-dsgvo-art-22',
      label: 'dsgvo-art-22',
      mimeType: MIME_PDF,
      pageCount: 4,
      sectionId: SEED_SECTION_LEGAL,
      sizeBytes: 88064,
      title: 'DSGVO-Auszug-Art-22.pdf',
    }),
    seedFileAsset({
      fileName: 'EU-AI-Act-Volltext.pdf',
      groupId: SEED_GROUP_EU_AI_ACT,
      id: 'file-asset-ai-act-volltext',
      label: 'ai-act-volltext',
      mimeType: MIME_PDF,
      pageCount: 144,
      sectionId: SEED_SECTION_LEGAL,
      sizeBytes: 2516582,
      title: 'EU-AI-Act-Volltext.pdf',
    }),
    seedFileAsset({
      fileName: 'AI-Act-Annex-III.pdf',
      groupId: SEED_GROUP_EU_AI_ACT,
      id: 'file-asset-ai-act-annex-iii',
      label: 'ai-act-annex-iii',
      mimeType: MIME_PDF,
      pageCount: 12,
      sectionId: SEED_SECTION_LEGAL,
      sizeBytes: 389120,
      title: 'AI-Act-Annex-III.pdf',
    }),
    ...seedLegalDemoFileAssets(),
    seedFileAsset({
      fileName: 'Perplexity-Enterprise-Datenblatt.pdf',
      id: 'file-asset-perplexity-db',
      label: 'perplexity-db',
      mimeType: MIME_PDF,
      pageCount: 8,
      sectionId: SEED_SECTION_MARKET,
      sizeBytes: 621568,
      title: 'Perplexity-Enterprise-Datenblatt.pdf',
    }),
    seedFileAsset({
      fileName: 'Azure-Foundry-WebSearch.pdf',
      id: 'file-asset-azure-foundry',
      label: 'azure-foundry',
      mimeType: MIME_PDF,
      pageCount: 6,
      sectionId: SEED_SECTION_MARKET,
      sizeBytes: 539648,
      title: 'Azure-Foundry-WebSearch.pdf',
    }),
    seedFileAsset({
      fileName: 'Anbieter-Vergleich-2026.xlsx',
      id: 'file-asset-anbieter-vergleich',
      label: 'anbieter-vergleich',
      mimeType: MIME_XLSX,
      sectionId: SEED_SECTION_MARKET,
      sizeBytes: 240640,
      title: 'Anbieter-Vergleich-2026.xlsx',
    }),
    seedFileAsset({
      fileName: 'Markt-Notizen.txt',
      id: 'file-asset-markt-notizen',
      label: 'markt-notizen',
      mimeType: MIME_TXT,
      sectionId: SEED_SECTION_MARKET,
      sizeBytes: 12288,
      title: 'Markt-Notizen.txt',
    }),
    {
      createdAt: seedCreatedAt,
      extractedText: 'Demo-1.docx Beispieltext für die Prompt-Library-Vorführung. Enthält Überschriften, Absätze und Aufzählungen, an denen sich die Fachübersetzung demonstrieren lässt.',
      fileName: 'Demo-1.docx',
      groupId: null,
      id: 'file-asset-demo-1',
      label: 'demo-1',
      mimeType: MIME_DOCX,
      origin: 'library',
      pageCount: 3,
      parseStatus: 'parsed',
      parseWarning: null,
      sectionId: SEED_SECTION_OWN,
      sizeBytes: 48128,
      textTruncated: false,
      title: 'Demo-1.docx',
      updatedAt: seedCreatedAt,
    },
    {
      createdAt: seedCreatedAt,
      extractedText: 'Demo-2.docx Beispieltext mit weiteren Absätzen und einer Tabelle, um Formatierung und Terminologie in der Übersetzung zu prüfen.',
      fileName: 'Demo-2.docx',
      groupId: null,
      id: 'file-asset-demo-2',
      label: 'demo-2',
      mimeType: MIME_DOCX,
      origin: 'library',
      pageCount: 22,
      parseStatus: 'partial',
      parseWarning: 'Textinhalt gekürzt — nur ein Teil des Dokuments wurde verarbeitet.',
      sectionId: SEED_SECTION_OWN,
      sizeBytes: 1887437,
      textTruncated: true,
      title: 'Demo-2.docx',
      updatedAt: seedCreatedAt,
    },
    {
      createdAt: seedCreatedAt,
      extractedText: 'Glossar-DE-EN: zweispaltige Terminologieliste (Deutsch/Englisch) mit bevorzugten Fachbegriffen für die Übersetzung.',
      fileName: 'Glossar-DE-EN.xlsx',
      groupId: null,
      id: 'file-asset-glossar',
      label: 'glossar',
      mimeType: MIME_XLSX,
      origin: 'library',
      pageCount: null,
      parseStatus: 'parsed',
      parseWarning: null,
      sectionId: SEED_SECTION_OWN,
      sizeBytes: 9472,
      textTruncated: false,
      title: 'Glossar-DE-EN.xlsx',
      updatedAt: seedCreatedAt,
    },
    seedFileAsset({
      fileName: 'Rechtsgutachten-Entwurf.docx',
      id: 'file-asset-rechtsgutachten',
      label: 'rechtsgutachten',
      mimeType: MIME_DOCX,
      pageCount: 18,
      sectionId: SEED_SECTION_OWN,
      sizeBytes: 320512,
      title: 'Rechtsgutachten-Entwurf.docx',
    }),
    {
      createdAt: seedCreatedAt,
      extractedText: 'Example vendor specification excerpt. EU data residency available in Frankfurt and Dublin. Audit logs retained for 365 days. Citation chains can be exported as Markdown.',
      fileName: 'vendor-spec-eu.txt',
      groupId: null,
      id: 'file-asset-vendor-spec',
      label: 'vendor-spec-eu',
      mimeType: MIME_TXT,
      origin: 'library',
      pageCount: null,
      parseStatus: 'parsed',
      parseWarning: null,
      sectionId: SEED_SECTION_OWN,
      sizeBytes: 184,
      textTruncated: false,
      title: 'vendor-spec-eu.txt',
      updatedAt: seedCreatedAt,
    },
  ]
}

function seedVectorIndexes(): VectorIndexRecord[] {
  // Chunk/vector counts are derived in the UI from pageCount (helpers.ts);
  // EU-Recht intentionally carries a large collection-sized member set so the
  // database demo exercises the dense explorer layout, not just a showcase list.
  // rechtsgutachten stays `pending` to show the "läuft" embedding state while
  // the index already serves its embedded members.
  const euRechtMembers = [
    'file-asset-ai-act-volltext',
    'file-asset-ai-act-annex-iii',
    'file-asset-bsi-kriterien',
    'file-asset-dsgvo-art-22',
    ...LEGAL_DEMO_DOCUMENTS.map((document) => `file-asset-${document.label}`),
  ].map((fileId) => ({ fileId, state: 'embedded' as const }))
  return [
    {
      createdAt: seedCreatedAt,
      dims: 3072,
      handle: 'eu-recht',
      id: 'vector-index-eu-recht',
      members: [
        ...euRechtMembers,
        { fileId: 'file-asset-rechtsgutachten', state: 'pending' },
      ],
      model: 'text-embedding-3-large',
      // A reindex is mid-flight on load (the demo "digital twin"): the
      // local simulator advances it to done, mirroring DEMO_RUNNING_RUN_ID.
      status: 'indexing',
      title: 'EU-Recht',
      updatedAt: seedCreatedAt,
    },
    {
      createdAt: seedCreatedAt,
      dims: 1536,
      handle: 'anbieter',
      // A few past runs so the inline history is visible without waiting.
      history: [
        { documents: 3, durationMs: 47_000, finishedAt: '2026-06-15T08:41:00.000Z', result: 'ok', startedAt: '2026-06-15T08:40:13.000Z' },
        { documents: 1, durationMs: 12_000, finishedAt: '2026-06-12T16:03:00.000Z', result: 'cancelled', startedAt: '2026-06-12T16:02:48.000Z' },
        { documents: 0, durationMs: 6_000, error: 'Embedding-Backend nicht erreichbar.', finishedAt: '2026-06-10T11:20:00.000Z', result: 'error', startedAt: '2026-06-10T11:19:54.000Z' },
      ],
      id: 'vector-index-anbieter',
      members: [
        { fileId: 'file-asset-perplexity-db', state: 'embedded' },
        { fileId: 'file-asset-azure-foundry', state: 'embedded' },
        { fileId: 'file-asset-bsi-kriterien', state: 'embedded' },
      ],
      model: 'text-embedding-3-small',
      // Seeded queued behind EU-Recht (see indexingJobs) to surface the
      // "In Warteschlange" state on load; the simulator promotes it.
      status: 'indexing',
      title: 'Anbieter-Wissen',
      updatedAt: seedCreatedAt,
    },
  ]
}

function seedEditorFolders(): EditorFolderRecord[] {
  return [
    {
      createdAt: seedCreatedAt,
      id: 'editor-folder-briefs',
      title: 'Berichte',
      updatedAt: seedCreatedAt,
    },
  ]
}

/** The Editor opens with a real research report loaded (the user's ask):
 * the chosen run's report, imported as an editable document. */
function seedEditorDocuments(runs: ResearchRunRecord[]): EditorDocumentRecord[] {
  const source =
    runs.find((run) => run.runId === DEMO_EDITOR_RUN_ID && run.result?.markdown)
    ?? runs.find((run) => run.status === 'completed' && run.result?.markdown)
  if (!source?.result?.markdown) return []
  const timestamp = source.finishedAt ?? source.submittedAt
  return [
    {
      contentMarkdown: source.result.markdown,
      createdAt: timestamp,
      folderId: 'editor-folder-briefs',
      id: 'editor-doc-demo-report',
      revision: 1,
      source: 'imported-research-report',
      sourceRunId: source.runId,
      title: 'EU AI Act – Open-Source-Modelle.md',
      updatedAt: timestamp,
    },
  ]
}

function seedEditorComments(): EditorCommentThreadRecord[] {
  return []
}

function seedResearchRuns(): ResearchRunRecord[] {
  // Real completed runs from the bundled project export, plus one live and one
  // queued job so the demo also shows the streaming/breathing card states.
  return [demoRunningRun(), demoQueuedRun(), ...parsedDemoRuns()]
}

/** A still-running job so the demo shows the live, breathing progress card the
 * way a real run streams — the simulative aspect kept on purpose. It is a
 * "happening now" simulation, so its timestamps are relative to the current
 * session (a few minutes ago) — not a stored creation date — which keeps the
 * runtime believable without revealing any past activity. */
function demoRunningRun(): ResearchRunRecord {
  const startMs = Date.now() - 230_000
  const at = (offsetSeconds: number) =>
    new Date(startMs + offsetSeconds * 1000).toISOString()
  const startedAt = at(0)
  return {
    agentOverrides: { first_round_queries: 6, max_rounds: 4, report_profile: 'deep' },
    createdAt: startedAt,
    events: [
      runEvent('RO-live-1', at(0), 'Analysiere Frage...'),
      runEvent('RO-live-2', at(20), 'Websuche erforderlich (Aktualität: diesen Monat)'),
      runEvent('RO-live-3', at(55), 'Analyseziele erkannt: 3 Teilfragen, 8 Pflichtaspekte'),
      runEvent('RO-live-4', at(90), 'Plane Suchanfragen (Runde 1/4)...'),
      runEvent('RO-live-5', at(125), '6 neue Suchanfragen generiert'),
      runEvent('RO-live-6', at(160), 'Durchsuche 6 Anfragen (Runde 1/4)...'),
      runEvent('RO-live-7', at(210), 'Bewerte Informationsqualität (nach Runde 1/4)...', true),
    ],
    metrics: {
      claims: 34,
      queries: 6,
      rounds: `1 / ${DEMO_RUNNING_MAX_ROUNDS}`,
      sources: 48,
    },
    phaseState: {
      activePhase: 'evaluation',
      completedPhases: ['analysis', 'planning', 'search'],
    },
    runId: DEMO_RUNNING_RUN_ID,
    source: 'mock',
    stack: 'anthropic_perplexity',
    startedAt,
    status: 'running',
    submittedAt: startedAt,
    summary: {
      title:
        'Welche neuen KI-Funktionen haben die großen Cloud-Anbieter im Juni 2026 angekündigt?',
    },
  }
}

/** A queued job, to show the waiting state in the demo. Also "now"-relative
 * (just submitted) for the same reason as the running run above. */
function demoQueuedRun(): ResearchRunRecord {
  const submittedAt = new Date(Date.now() - 60_000).toISOString()
  return {
    agentOverrides: { first_round_queries: 6, max_rounds: 10, report_profile: 'compact' },
    createdAt: submittedAt,
    events: [],
    metrics: { claims: 0, queries: 0, rounds: '0 / 10', sources: 0 },
    phaseState: { activePhase: 'analysis', completedPhases: [] },
    runId: 'RO-queued-9002',
    source: 'mock',
    stack: 'azure_web_search',
    status: 'queued',
    submittedAt,
    summary: {
      queueNote: 'Startet in ~2 Min',
      title: 'Vergleich der EU-Cloud-Anbieter für vertrauliche Recherche-Workflows.',
    },
  }
}

function seedChatThreads(): ChatThreadRecord[] {
  return parsedDemoChats()
}

function runEvent(
  id: string,
  createdAt: string,
  title: string,
  active = false,
) {
  return {
    active: active || undefined,
    createdAt,
    id,
    kind: 'progress' as const,
    severity: title.toLowerCase().includes('contradiction') ? 'warning' as const : 'info' as const,
    title,
  }
}
