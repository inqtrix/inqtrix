import type {
  VerificationEngine,
  VerificationProfile,
} from './model.ts'

export const MOBILE_SCENARIO_TAG = '@mobile'
export const MOBILE_ONLY_SCENARIO_TAG = '@mobile-only'
export const CHROMIUM_ONLY_SCENARIO_TAG = '@chromium-only'

export const COLLABORATION_BROWSER_TARGETS = [
  { browser: 'chromium', formFactor: 'desktop' },
  { browser: 'chromium', formFactor: 'mobile' },
  { browser: 'firefox', formFactor: 'desktop' },
  { browser: 'webkit', formFactor: 'desktop' },
] as const

export type CollaborationBrowserTarget =
  typeof COLLABORATION_BROWSER_TARGETS[number]

export type ScenarioDefinition = {
  browsers?: readonly CollaborationBrowserTarget['browser'][]
  cleanup: string
  destructive: boolean
  engine: VerificationEngine
  formFactors?: readonly ('desktop' | 'mobile')[]
  id: string
  prerequisites: readonly string[]
  profiles: readonly VerificationProfile[]
  selectorTag?: `@${string}`
  testTitle?: string
  title: string
}

export const PROFILE_ENGINE_ORDER: Readonly<
  Record<VerificationProfile, readonly VerificationEngine[]>
> = {
  'agent-desk': ['agent-desk-live'],
  'agent-recovery': ['agent-desk-live'],
  'chat-prompt': ['chat-prompt-live'],
  'edge-conformance': ['web-edge-containers'],
  'fault-injection': ['collaboration-playwright'],
  'load-capacity': ['collaboration-load'],
  'load-ramp': ['collaboration-load'],
  'load-smoke': ['collaboration-load'],
  'load-soak': ['collaboration-load'],
  'owner-setup': ['owner-setup-live'],
  'system-smoke': ['collaboration-playwright', 'editor-system-live'],
  'ui-fixture': ['ui-fixture-playwright'],
}

export const PROFILE_DESCRIPTIONS: Readonly<Record<VerificationProfile, string>> = {
  'agent-recovery': 'Controlled provider faults through the existing kernel, visible failure/recovery and scoped cleanup.',
  'agent-desk': (
    'Kernel answer experience through the visible Agent Desk against '
    + 'the live stack: run submission, tool activity, clickable '
    + 'citations, real research follow-ups, synthesis with web disabled, '
    + 'a Canvas report from conversation history, browser hygiene, and cleanup.'
  ),
  'chat-prompt': (
    'Chat and Prompt Library browser matrix against the live stack: '
    + 'real completions, reload ordering, duplicated tabs, offline '
    + 'recovery, and library CRUD in Chromium, Firefox, and WebKit.'
  ),
  'edge-conformance': 'Black-box parity of the packaged Python and nginx web edges.',
  'fault-injection': 'Controlled collaboration failure and recovery scenarios.',
  'load-capacity': 'Fixed high-capacity collaboration workload and resilience budgets.',
  'load-ramp': (
    'Local, resource-gated socket fan-out ladder (20/100/250/500/1000) '
    + 'from a capped identity pool. A controlled stop reports the highest '
    + 'reached rung; it never discharges the production capacity gate.'
  ),
  'load-smoke': (
    'Run-ID-bound 20-socket collaboration protocol, visibility, and '
    + 'durability workload with automatic temporary-resource provisioning.'
  ),
  'load-soak': (
    'Thirty-minute, 25-identity mixed collaboration soak with phased '
    + 'network shaping, real product APIs, and resource recovery budgets.'
  ),
  'owner-setup': (
    'One visible first-run local-owner session through setup, initial '
    + 'persistence, user administration, and pointer logout on a fresh stack.'
  ),
  'system-smoke': 'External multiuser system behavior through visible browser flows.',
  'ui-fixture': 'Deterministic browser fixtures without external product services.',
}

export const SCENARIO_INVENTORY: readonly ScenarioDefinition[] = [
  ...([
    ['conversation-followups', 'Real-provider research follow-ups complete in the same saved conversation'],
    ['conversation-history-synthesis', 'A subsequent conversation synthesis completes with web disabled'],
    ['conversation-history-canvas', 'A subsequent turn creates a ready Canvas report from the conversation'],
    ['conversation-firefox', 'Firefox reads the same persisted conversation and Canvas planning facts'],
    ['conversation-webkit', 'WebKit reads the same persisted conversation and Canvas planning facts'],
    ['conversation-knowledge-document', 'The real conversation reads the selected knowledge document with web disabled'],
    ['conversation-editor-document', 'The real conversation reads and searches the selected editor document with web disabled'],
    ['conversation-history-material', 'A later turn answers from the saved transcripts of the document turns without reopening the documents'],
    ['conversation-file-attachment', 'The real conversation reads an uploaded PDF from its canonical server extraction, whose facts name the page without text'],
    ['conversation-review-role', 'A Deep turn hands its outputs to the review role: one bounded child run with its profile tools only, a typed result, a read basis and no answer of its own'],
    ['conversation-mission-review', 'A mission turn is reviewed through the shared review pass: a visible decision, every planned criterion settled by its number, a named transport with a role child exactly where the role reviewed, and a memo that cites only its own references'],
  ] as const).map(([id, title]): ScenarioDefinition => ({
    id: `agent.${id}`, title, engine: 'agent-desk-live', profiles: ['agent-desk'], destructive: true,
    prerequisites: ['Healthy canonical stack with real providers', 'Authenticated tester'],
    cleanup: 'The existing session/run ledger owns every conversation turn and report. Semantic acceptance additionally requires review of the visible result and trace content.',
  })),
  ...([
    ['context', 'Context rejection preserves and projects the full source'],
    ['output-limit', 'Output configuration failure is not retried as context overflow'],
    ['arguments', 'Malformed arguments are corrected through the same model and tool guards'],
    ['arguments-exhausted', 'Repeated malformed arguments stop at the shared attempt limit'],
    ['transport', 'Transient statuses share the provider retry budget'],
    ['access-denied', 'Provider access denial remains terminal'],
    ['unknown', 'Unknown internal failure never becomes a successful empty result'],
    ['cancel', 'Cancellation prevents the next provider retry'],
    ['context-unreducible', 'Context without safely reducible tool data fails visibly'],
    ['archive-window', 'A versioned archive tail read supplies a fact absent from the first window'],
    ['archive-catalogue', 'Catalogue continuation reaches a source beyond the first page'],
    ['conversation-originals', 'The real model retrieves requirements beyond the saved history preview'],
    ['bulk-handover', 'A full tool handover preserves a decisive fact after 60000 characters'],
    ['normal-memo', 'An ordinary provider run produces a factually correct decision memo'],
    ['browser-hygiene', 'Recovery scenarios produce no unexplained browser errors'],
    ['firefox', 'Firefox renders persisted recovery, failure and cancellation'],
    ['webkit', 'WebKit renders persisted recovery, failure and cancellation'],
  ] as const).map(([id, title]): ScenarioDefinition => ({
    id: `agent.recovery.${id}`,
    title,
    engine: 'agent-desk-live', profiles: ['agent-recovery'], destructive: true,
    prerequisites: ['Healthy idle canonical local stack', 'Explicit Podman control', 'Authenticated tester'],
    cleanup: 'Owned sessions and runs use the shared ledger; the normal API/worker commands are restored.',
  })),
  uiScenario(
    'ui.lifecycle-isolation',
    'Document rerender isolation',
    'A to B rerender never exposes A through B surface or controller registration',
  ),
  uiScenario(
    'ui.modal-focus-race',
    'Modal launcher teardown focus',
    'modal focus wins the launcher teardown race and restores on Escape',
  ),
  uiScenario(
    'ui.editor-share-focus',
    'Editor share dialog focus handoff',
    'editor share menu returns focus after Escape and click-outside',
  ),
  uiScenario(
    'ui.explorer-action-tooltip',
    'Explorer action tooltip handoff',
    'explorer action tooltip yields while its modal owns interaction',
  ),
  uiScenario(
    'ui.mobile-collaboration-presence',
    'Bounded mobile collaboration identity labels',
    'long collaboration labels stay inside the mobile editor at both edges',
  ),
  uiScenario(
    'ui.profile-menu-tooltip',
    'Profile menu and tooltip ownership',
    'profile menu suppresses its trigger tooltip while actions are open',
  ),
  uiScenario(
    'ui.activation-hydration',
    'Activation and exact hydration ordering',
    'activation success closes the writable body window before delayed exact hydration',
  ),
  uiScenario(
    'ui.narrow-topbar',
    'Narrow topbar bounded layout',
    'narrow topbar keeps long title and four-participant status in separate bounded tracks',
  ),
  uiScenario(
    'ui.markdown-rendering',
    'Markdown rendering sample set',
    'rendering sample set draws every construct bounded, transparent and themed',
  ),
  uiScenario(
    'ui.save-status',
    'Stable save-status acknowledgement',
    'save status suppresses acknowledgement flicker and keeps its label track stable',
  ),
  uiScenario(
    'ui.recovery-copy-notice',
    'The unsaved state of a replaced generation opens as a recovery copy',
    'the unsaved state of a replaced generation is announced on its document and opens as a recovery copy',
  ),
  uiScenario(
    'ui.initial-suggest-policy',
    'Initial Suggest transaction policy',
    'initial and same-event Suggest policies govern the first document transaction',
  ),
  uiScenario(
    'ui.suggestion-undo',
    'Rejected Suggest undo retry',
    'rejected Suggest undo emits no Yjs mutation and remains retryable',
  ),
  uiScenario(
    'ui.simple-markup',
    'Simple Markup compound suggestions',
    'Simple Markup reveals every active compound suggestion and hides other deletions',
  ),
  uiScenario(
    'ui.slash-heading',
    'Schema-valid slash heading',
    'slash heading creates a server-valid collaboration document',
  ),
  uiScenario(
    'ui.slash-matrix',
    'Schema-valid slash command matrix',
    'all eleven slash actions keep the collaboration document schema-valid',
  ),
  uiScenario(
    'ui.slash-keyboard',
    'Slash search and keyboard behavior',
    'slash search, keyboard selection, escape, and suggest guards are coherent',
  ),
  uiScenario(
    'ui.slash-reversible',
    'Reversible slash suggestion updates',
    'all reversible slash suggestions emit complete server-valid Yjs V1 updates',
  ),
  uiScenario(
    'ui.comment-density',
    'Sixty-thread inspector density',
    '60-thread inspector stays compact, progressive, and single-composer',
  ),
  uiScenario(
    'ui.view-only-assistant',
    'View-only assistant restriction',
    'view-only assistant controls explain the restriction and never dispatch work',
  ),
  uiScenario(
    'ui.assistant-answer-per-document',
    'Editor assistant answer per document',
    'the editor assistant answer stays with the document it was asked in',
  ),
  uiScenario(
    'ui.assistant-names-replaced-suggestions',
    'Editor assistant names the open suggestions a new instruction replaces',
    'an open AI suggestion is named before a new instruction replaces it, in its own document only',
  ),
  uiScenario(
    'ui.assistant-ai-disclosure',
    'Editor assistant carries the AI disclosure under its input',
    'the editor assistant says under its input that answers come from an AI system, also while typing',
  ),
  uiScenario(
    'ui.mobile-file-upload-action',
    'Mobile file-library upload action',
    'mobile file library keeps its primary upload action fully visible',
  ),
  uiScenario(
    'ui.accessibility-demo',
    'Automated WCAG A/AA scan across demo workspaces',
    'demo workspaces have no automated WCAG A or AA violations',
  ),
  uiScenario(
    'ui.chat-empty-session',
    'Localized empty chat creation',
    'new chats start as localized empty sessions in both UI languages',
  ),
  uiScenario(
    'ui.appearance-presets',
    'Appearance presets persist every choice under a fixed brand mark',
    'appearance presets save each choice and keep a fixed brand mark',
  ),
  uiScenario(
    'ui.lock-screen-modes',
    'Lock screen in every authentication mode, language and viewport',
    'lock screen stays usable in each authentication mode, language and viewport',
  ),
  uiScenario(
    'ui.brand-focus-visible',
    'Visible keyboard focus on brand-filled lock screen controls in every preset',
    'keyboard focus stays visible on brand-filled lock screen controls in every preset',
  ),
  uiScenario(
    'ui.brand-mark-presets',
    'Fixed brand mark in every preset and the high-contrast lock screen',
    'every preset keeps the fixed logo and high contrast overrides the lock screen',
  ),
  uiScenario(
    'ui.brand-mark-small-surfaces',
    'Crisp 24px brand mark on owner setup and guest access',
    'owner setup and guest access use a crisp 24px mark',
  ),
  uiScenario(
    'ui.editor-open-no-body',
    'Opening the live editor hands its host no body',
    'opening, focusing and clicking the live editor never hands its host a body',
  ),
  uiScenario(
    'ui.editor-late-normalization',
    'A late trailing paragraph is no content change',
    'a trailing paragraph appended after mount is no content change',
  ),
  uiScenario(
    'ui.editor-input-bodies',
    'Typed, pasted and composed text reach the host',
    'typing, pasting and composing text each hand the host the new body',
  ),
  uiScenario(
    'ui.editor-undo-stored-bytes',
    'Undo to the loaded text returns its stored bytes',
    'undo back to the loaded text hands the host its stored bytes and keeps the caret',
  ),
  uiScenario(
    'ui.editor-undo-first-typing',
    'Undo right after opening takes back only the typed text',
    'text typed right after opening is undone on its own and keeps the caret',
  ),
  uiScenario(
    'ui.canvas-save-citations',
    'Canvas saves real edits and keeps citations',
    'the canvas saves a real edit once, never on open or undo, and keeps its citations',
  ),
  uiScenario(
    'ui.canvas-edit-before-last-block',
    'A canvas edit before any last block saves only that edit',
    'a canvas edit before every kind of last block saves that edit and nothing else',
  ),
  uiScenario(
    'ui.canvas-citation-chips',
    'Editing a canvas shows its citations as chips that open their evidence',
    'editing a canvas shows a chip for every citation the reading view links, and a chip opens its evidence',
  ),
  uiScenario(
    'ui.canvas-chip-keyboard',
    'Enter inside a citation chip opens its evidence',
    'Enter inside a chip opens its evidence, and the caret moves through a chip as through text',
  ),
  uiScenario(
    'ui.canvas-escaped-labels',
    'Escaped canvas labels heal on the next edit',
    'a canvas stored with escaped labels heals on the next edit, never on open',
  ),
  uiScenario(
    'ui.canvas-late-save-answer',
    'A late save answer keeps newer typing',
    'a save answered after further typing keeps the newer text and the caret',
  ),
  uiScenario(
    'ui.canvas-save-answer-after-undo',
    'A save answer after an undo keeps the loaded text',
    'a save answered after an undo keeps the loaded text',
  ),
  uiScenario(
    'ui.editor-editability-headers',
    'Code block controls follow the right to edit',
    'the code block language picker follows the right to edit',
  ),
  uiScenario(
    'ui.editor-reconnect-keeps-editor',
    'A reconnect keeps the editor, its text, focus and undo history',
    "a reconnect keeps the writer's editor, text, focus and undo history and leaves the other writer connected",
  ),
  uiScenario(
    'ui.editor-reconnect-composition',
    'A composition across a reconnect is committed once',
    'an IME composition that spans a reconnect is committed once and keeps focus',
    // The composition is driven through the Chromium DevTools protocol.
    ['chromium'],
  ),
  uiScenario(
    'ui.editor-reconnect-focus-elsewhere',
    'A reconnect leaves a selection made elsewhere alone',
    'a reconnect does not take the focus back after the user selected text elsewhere',
  ),
  uiScenario(
    'ui.editor-send-slow-store',
    'A writer on a slow store keeps one update in flight and loses nothing',
    'a writer who keeps typing while the store is slow stays connected, keeps one update in flight and loses no character',
  ),
  uiScenario(
    'ui.editor-send-three-writers',
    'Three writers in one paragraph stay connected and converge',
    'three writers typing at human pace in one paragraph stay connected, converge and store every character',
  ),
  uiScenario(
    'ui.editor-empty-line-first-letters',
    'Two writers starting the same empty line both stay connected',
    'two writers who click into the same empty line and type at the same time stay connected, see no notice and keep both words',
  ),
  uiScenario(
    'ui.editor-empty-line-words',
    'Two writers typing words into the same empty line keep them whole',
    'two writers who keep typing words into the same empty line at human pace stay connected, see no notice and keep every word whole',
  ),
  uiScenario(
    'ui.editor-enter-line-words',
    'Two writers typing words into a line Enter just created keep them whole',
    'a writer who presses Enter and a writer who clicks into the new line and both type words at human pace stay connected, see no notice and keep every word whole',
  ),
  uiScenario(
    'ui.editor-send-suggest-corrections',
    'Suggest-mode corrections on a slow store are all stored',
    'a suggest-mode writer who corrects at human pace while the store is slow stays connected and every accepted suggestion is stored',
  ),
  uiScenario(
    'ui.editor-send-mode-switches',
    'Mode switches while an update waits are never rejected',
    'an edit-access writer who switches modes and edits inside its own suggestion while an update waits is never rejected',
  ),
  uiScenario(
    'ui.editor-write-mode-linger',
    'A reopened lingering editor keeps its suggest mode',
    'an editor reopened in suggest mode on a lingering document merges its suggestion typing',
  ),
  uiScenario(
    'ui.editor-write-mode-other-document',
    'An editor on another document keeps its suggest mode',
    'an editor opened on another document in suggest mode merges its suggestion typing there',
  ),
  uiScenario(
    'ui.editor-write-mode-reopened-edit',
    'An editor reopened in edit mode sends its changes apart',
    'an editor reopened in edit mode after suggest mode sends a plain change and a change inside its own suggestion apart',
  ),
  uiScenario(
    'ui.editor-page-exit-typing',
    'Typing just before a reload is stored',
    'every keystroke typed within one acknowledgement cycle before a reload is stored',
  ),
  uiScenario(
    'ui.editor-page-exit-suggestions',
    'Suggestions typed just before a reload are stored',
    'every suggestion typed within one acknowledgement cycle before a reload is stored',
  ),
  uiScenario(
    'ui.editor-page-exit-leave-question',
    'A page that cannot send everything asks before a reload',
    'a page that holds more changes than it may send at once asks before a reload and stores what it sent',
  ),
  uiScenario(
    'ui.editor-concurrent-bold',
    'Two writers bolding the same word both keep writing',
    'two writers who bold the same word at the start of a paragraph at the same time both keep writing',
  ),
  uiScenario(
    'ui.editor-ai-accept-while-typing',
    'An AI suggestion accepted while another writer types is published on the first click',
    'an AI suggestion accepted while another writer types is published on the first click, and what the other writer typed stays exactly once',
  ),
  uiScenario(
    'ui.editor-ai-accept-passage-changed',
    'An AI suggestion whose passage changed while it was accepted ends calmly as changed',
    'an AI suggestion whose passage another writer changes while it is accepted ends calmly as changed, and nothing is published',
  ),
  uiScenario(
    'ui.editor-ai-reject-carrier',
    'Rejecting an AI suggestion deletes its carrier row',
    'rejecting an AI suggestion in a shared document deletes its carrier row',
  ),
  uiScenario(
    'ui.editor-ai-carrier-unmarked',
    'A carrier row never marks the text, a note does',
    'a carrier never marks the text, a note does',
  ),
  uiScenario(
    'ui.editor-ai-reject-note',
    'Rejecting an AI suggestion of a note keeps the note',
    'rejecting an AI suggestion of a note keeps the note and its mark',
  ),
  uiScenario(
    'ui.editor-ai-reject-stale',
    'A stale AI suggestion is rejected by its own button',
    'a stale AI suggestion is rejected by its own button, and its carrier row goes with it',
  ),
  uiScenario(
    'ui.editor-ai-reject-all-stale',
    'Rejecting all AI suggestions also rejects stale ones',
    'rejecting all AI suggestions also rejects stale ones, with every carrier row',
  ),
  uiScenario(
    'ui.editor-rejected-change-reload',
    'A rejected change stays as a backup until the document is reloaded',
    'a change the service rejects keeps the local state as a read-only backup until the writer reloads the document, and the reload starts over from the service',
  ),
  uiScenario(
    'ui.editor-refused-split',
    'A suggest-mode Enter mid-paragraph is refused locally with a German reason',
    'a suggest-mode Enter in the middle of a paragraph is refused in the editor with a German reason, sends nothing and leaves the session writable',
  ),
  uiScenario(
    'ui.editor-refused-adjacent-foreign',
    'Typing behind a foreign suggestion is refused locally with a German reason',
    "typing in suggest mode directly behind another person's pending suggestion is refused in the editor with a German reason and leaves the session writable",
  ),
  uiScenario(
    'ui.editor-suggest-catalog-new-blocks',
    'Every suggest-mode new-block action is stored or refused locally',
    'suggest mode with real keys: every new-block action is either stored by the service or refused in the editor with a German reason',
  ),
  uiScenario(
    'ui.editor-suggest-catalog-deletions',
    'Every suggest-mode deletion action is stored or refused locally',
    'suggest mode with real keys: every deletion action is either stored by the service or refused in the editor with a German reason',
  ),
  uiScenario(
    'ui.editor-suggest-catalog-formatting',
    'Every suggest-mode formatting and block action is stored or refused locally',
    'suggest mode with real keys: every formatting and block action is either stored by the service or refused in the editor with a German reason',
  ),
  uiScenario(
    'ui.editor-suggest-catalog-paste',
    'Every suggest-mode paste and automatic-formatting action is stored or refused locally',
    'suggest mode with real keys: every paste, whole-document and automatic-formatting action is either stored by the service or refused in the editor with a German reason',
  ),
  uiScenario(
    'ui.editor-suggest-enter-at-block-end',
    'A suggest-mode Enter at a block end proposes the new block',
    'a suggest-mode Enter at the end of a paragraph, a list item and a task item proposes the new block, and the other writer sees it',
  ),
  uiScenario(
    'ui.editor-suggest-open-writes-nothing',
    'Opening a document that ends in a list or heading writes nothing',
    'a suggest-mode writer who opens a document that ends in a list or a heading sends nothing on opening and keeps suggesting',
  ),
  uiScenario(
    'ui.editor-reconnect-silent-path',
    'A silently dead connection loses nothing',
    'a connection that dies silently loses nothing the writer typed before or after it: its unacknowledged update is sent again and acknowledged on the next connection',
  ),
  uiScenario(
    'ui.editor-reconnect-unanswered-connect',
    'An unanswered connection is given up and replaced',
    'a connection whose first answer never arrives is given up and replaced, and the writer keeps writing',
  ),
  uiScenario(
    'ui.editor-reconnect-presence-takeover',
    'A reconnect keeps presence without a close loop',
    'a writer who reconnects while the service still holds the old connection keeps its presence for the others, and the room refuses none of its presence frames',
  ),
  uiScenario(
    'ui.editor-reconnect-sync-cleanup-edit',
    'After a reconnect an edit writer sends the formatting cleanup of its sync answer, and the service accepts it',
    'a writer with edit access who reconnects after a room change left a format item redundant sends the cleanup, which the service accepts, and a bold letter typed there reads the same everywhere',
  ),
  uiScenario(
    'ui.editor-reconnect-sync-cleanup-suggest',
    'After a reconnect a suggest writer sends the formatting cleanup of its sync answer, and the service accepts it',
    'a writer with suggest access who reconnects after a room change left a format item redundant sends the cleanup, which the service accepts, and a bold letter typed there reads the same everywhere',
  ),
  uiScenario(
    'ui.editor-caret-presence-continuity',
    "Another writer's caret stays on screen while they type",
    "another writer's caret and its name stay on screen, as one element, in every frame while that writer types at an irregular pace against a slow store",
  ),
  uiScenario(
    'ui.editor-edit-shrinks-suggestion',
    'An edit that deletes part of a pending suggestion leaves the rest pending',
    "an edit that deletes part of another writer's pending insertion leaves the rest pending under its author, and undo gives the characters back as the editor's own text",
  ),
  uiScenario(
    'ui.editor-edit-types-inside-suggestion',
    'Text typed inside a pending suggestion is the editor\'s own, and accepting it applies both parts',
    "text an edit types inside another writer's pending insertion is the editor's own, and a third writer who accepts the insertion applies both of its parts",
  ),
  uiScenario(
    'ui.editor-edit-removes-suggestion-history',
    'An edit that deletes a whole pending suggestion removes it, visibly in the history',
    "an edit that deletes all of another writer's pending insertion removes it, both writers read in the history who removed whose suggestion, and undo keeps the removal and the editor's earlier steps",
  ),
  uiScenario(
    'ui.editor-edit-removes-replacement',
    'An edit that types inside a proposed replacement removes it without a word',
    "an edit that types inside another writer's proposed replacement removes the replacement without a word, both writers read in the history who removed whose suggestion, and undo keeps the removal",
  ),
  uiScenario(
    'ui.editor-edit-withdraws-own-suggestion',
    'An edit that deletes the editor\'s own pending suggestion withdraws it',
    "an edit that deletes a sentence holding the editor's own pending insertion withdraws it without a word",
  ),
  uiScenario(
    'ui.editor-undo-keeps-own-withdrawal',
    'Undo after withdrawing an own pending suggestion by an edit keeps the withdrawal',
    "undo after an edit that withdrew the editor's own pending insertion brings back the deleted text and keeps the withdrawal without a word",
  ),
  uiScenario(
    'ui.editor-edit-moves-proposed-block',
    'An edit that moves a block with a proposed heading removes the proposal without a word',
    "an edit that moves a block carrying another writer's proposed heading removes the proposal without a word, and both writers read in the history who removed whose suggestion",
  ),
  uiScenario(
    'ui.editor-table-cell-line-break',
    'A line break in a table cell reads as a line break for every writer and stays stored',
    'a line break inside a table cell reads as a line break for every writer of an activated document, and the store keeps it after an edit',
  ),
  uiScenario(
    'ui.editor-edit-formatting-applies-to-suggestion',
    'Edit-mode formatting applies to a pending suggestion, which stays pending',
    "bold in edit mode across another writer's pending insertion formats all of it, and the insertion stays pending under its author for both writers",
  ),
  uiScenario(
    'ui.editor-edit-code-leaves-suggestion',
    'Inline code leaves a pending suggestion out, and a heading keeps it pending',
    "inline code in edit mode across another writer's pending insertion leaves the insertion out without a word, and a heading of its paragraph keeps it pending",
  ),
  uiScenario(
    'ui.editor-caret-label-rest-contrast',
    'A resting caret label is idle and readable on bright colours',
    'a resting caret label writes no style, and a writer with a bright colour gets dark label text',
  ),
  {
    cleanup: 'The externally prepared run-scoped stack and its volumes are removed after the browser report is retained.',
    destructive: true,
    engine: 'owner-setup-live',
    id: 'auth.owner-setup-visible',
    prerequisites: [
      'Fresh loopback-bound local-auth stack with no owner',
      'One explicit Playwright browser target',
      'Protected synthetic owner credentials',
    ],
    profiles: ['owner-setup'],
    title: 'Visible owner setup creates an authenticated administrator session',
  },
  {
    cleanup: 'Covered by removal of the externally prepared run-scoped stack and volumes.',
    destructive: true,
    engine: 'owner-setup-live',
    id: 'auth.initial-server-mutation',
    prerequisites: ['The newly created owner session without a page reload'],
    profiles: ['owner-setup'],
    title: 'The first project mutation persists and the owning sync badge confirms success',
  },
  {
    cleanup: 'The synthetic account is removed with the externally prepared run-scoped stack and volumes.',
    destructive: true,
    engine: 'owner-setup-live',
    id: 'auth.admin-user-create',
    prerequisites: [
      'The authenticated owner session',
      'Protected credentials for a distinct synthetic user',
    ],
    profiles: ['owner-setup'],
    title: 'The owner creates a distinct local user through the visible administration UI',
  },
  {
    cleanup: 'Logout destroys the only browser session; the external stack cleanup removes retained audit data.',
    destructive: true,
    engine: 'owner-setup-live',
    id: 'auth.pointer-logout',
    prerequisites: ['The authenticated owner profile menu'],
    profiles: ['owner-setup'],
    title: 'A pointer-driven profile logout destroys exactly one server session',
  },
  {
    cleanup: 'Observational scenario with no resources of its own.',
    destructive: false,
    engine: 'owner-setup-live',
    id: 'auth.console-network-clean',
    prerequisites: ['The complete owner-setup browser flow'],
    profiles: ['owner-setup'],
    title: 'The first-run flow has no unexplained console errors, failed requests, CSRF rejection, or server error',
  },
  collaborationScenario(
    'system.transport-fingerprint',
    'Observable transport identity',
    '@transport-fingerprint',
    ['system-smoke'],
  ),
  collaborationScenario(
    'system.direct-edit',
    'Direct edit visibility and durable persistence',
    '@direct-edit',
    ['system-smoke'],
  ),
  collaborationScenario(
    'system.large-state-latency',
    'Large collaboration state remains responsive under concurrent visible edits',
    '@large-state-latency',
    ['system-smoke'],
    ['desktop'],
    false,
    ['chromium'],
  ),
  collaborationScenario(
    'system.remote-caret',
    'Remote caret identity at the author position',
    '@remote-caret',
    ['system-smoke'],
    ['desktop'],
  ),
  collaborationScenario(
    'system.remote-selection',
    'Remote selection presentation',
    '@remote-selection',
    ['system-smoke'],
    ['desktop'],
  ),
  collaborationScenario(
    'system.concurrent-edits',
    'Concurrent edits converge exactly once',
    '@concurrent-edits',
    ['system-smoke'],
  ),
  collaborationScenario(
    'system.ai-suggestion-accept',
    'An assistant suggestion publishes and reaches the second session',
    '@ai-suggestion-accept',
    ['system-smoke'],
    ['desktop'],
  ),
  collaborationScenario(
    'system.collaboration-stays-connected',
    'Two writing sessions stay connected without a reconnect banner',
    '@stays-connected',
    ['system-smoke'],
  ),
  collaborationScenario(
    'system.suggestions',
    'Suggestion acceptance and rejection',
    '@suggestions',
    ['system-smoke'],
  ),
  collaborationScenario(
    'system.suggestion-undo',
    'Suggestion undo remains connected and durable',
    '@suggestion-undo',
    ['system-smoke'],
    ['desktop'],
  ),
  collaborationScenario(
    'system.ime',
    'Genuine browser IME collaboration',
    '@ime',
    ['system-smoke'],
    ['desktop', 'mobile'],
    false,
    ['chromium'],
  ),
  collaborationScenario(
    'system.source-readonly',
    'Read-only Source projection',
    '@source-readonly',
    ['system-smoke'],
  ),
  collaborationScenario(
    'system.layout',
    'Bounded editor and Inspector layout',
    '@layout',
    ['system-smoke'],
  ),
  collaborationScenario(
    'system.mobile-drawers',
    'Exclusive mobile tree and Inspector drawers',
    '@mobile-drawer',
    ['system-smoke'],
    ['mobile'],
  ),
  {
    cleanup: 'The engine deletes run-prefixed documents, closes contexts, and erases temporary users in finally.',
    destructive: true,
    engine: 'editor-system-live',
    id: 'system.multiuser-live-matrix',
    prerequisites: [
      'External product stack',
      'Two authenticated seed accounts',
      'Admin user-management permission',
    ],
    profiles: ['system-smoke'],
    title: 'Six-user document, permission, comment, and responsive-layout matrix',
  },
  {
    cleanup: 'The run-question-bound agent session and its runs are registered on creation and deleted through owner APIs.',
    destructive: true,
    engine: 'agent-desk-live',
    id: 'agent.kernel-run-submits',
    prerequisites: [
      'External product stack with the agent kernel enabled',
      'Authenticated tester seed account',
    ],
    profiles: ['agent-desk'],
    title: 'A composer submission creates a session and a running kernel run',
  },
  {
    cleanup: 'Covered by the shared agent session/run cleanup registration.',
    destructive: true,
    engine: 'agent-desk-live',
    id: 'agent.tool-activity-visible',
    prerequisites: ['A running kernel run that performs a source-tool call'],
    profiles: ['agent-desk'],
    title: 'Tool activity appears live with the literal query and settles',
  },
  {
    cleanup: 'Covered by the shared agent session/run cleanup registration.',
    destructive: true,
    engine: 'agent-desk-live',
    id: 'agent.answer-citations-clickable',
    prerequisites: ['A completed kernel answer with evidence references'],
    profiles: ['agent-desk'],
    title: 'The chat answer renders citation chips and a source list that open',
  },
  {
    cleanup: 'No resources of its own; observational over the shared run.',
    destructive: false,
    engine: 'agent-desk-live',
    id: 'agent.console-network-clean',
    prerequisites: ['The shared agent-desk run'],
    profiles: ['agent-desk'],
    title: 'Zero console errors, failed requests, and 5xx responses across the flow',
  },
  {
    cleanup: 'Asserts the terminal run state and complete cleanup registration itself.',
    destructive: false,
    engine: 'agent-desk-live',
    id: 'agent.cleanup-integrity',
    prerequisites: ['The shared agent-desk run'],
    profiles: ['agent-desk'],
    title: 'Every created agent resource is registered and terminal before exit',
  },
  {
    cleanup: 'The run-titled thread is registered on creation and deleted through the visible UI; the ledger pass is 404-tolerant.',
    destructive: true,
    engine: 'chat-prompt-live',
    id: 'chat.live-turn-roundtrip',
    prerequisites: [
      'External product stack',
      'Authenticated tester seed account',
    ],
    profiles: ['chat-prompt'],
    title: 'A visible chat question produces a persisted assistant answer in Chromium, Firefox, and WebKit',
  },
  {
    cleanup: 'Covered by the shared chat-thread cleanup registration.',
    destructive: false,
    engine: 'chat-prompt-live',
    id: 'chat.reload-turn-order',
    prerequisites: ['The shared per-browser chat thread'],
    profiles: ['chat-prompt'],
    title: 'A real reload preserves the user-before-assistant turn order in every browser',
  },
  {
    cleanup: 'Covered by the shared chat-thread cleanup registration.',
    destructive: false,
    engine: 'chat-prompt-live',
    id: 'chat.duplicate-tab-consistency',
    prerequisites: ['The shared per-browser chat thread'],
    profiles: ['chat-prompt'],
    title: 'A duplicated tab reads the same thread consistently without duplicate sends',
  },
  {
    cleanup: 'Covered by the shared chat-thread cleanup registration.',
    destructive: true,
    engine: 'chat-prompt-live',
    id: 'chat.network-recovery',
    prerequisites: ['The shared per-browser chat thread'],
    profiles: ['chat-prompt'],
    title: 'An offline send fails visibly without losing input and completes after recovery',
  },
  {
    cleanup: 'Asserts the visible thread deletion and zero server residue itself.',
    destructive: true,
    engine: 'chat-prompt-live',
    id: 'chat.cleanup-integrity',
    prerequisites: ['The shared per-browser chat thread'],
    profiles: ['chat-prompt'],
    title: 'The visible thread deletion leaves zero run-bound chat residue per browser',
  },
  {
    cleanup: 'The run-titled template is registered on creation and deleted through the visible UI; the ledger pass is 404-tolerant.',
    destructive: true,
    engine: 'chat-prompt-live',
    id: 'prompt.live-owner-crud',
    prerequisites: [
      'External product stack',
      'Authenticated tester seed account',
    ],
    profiles: ['chat-prompt'],
    title: 'Prompt-template create, edit, revision, reload, and search work in Chromium, Firefox, and WebKit',
  },
  {
    cleanup: 'Covered by the shared prompt-template cleanup registration.',
    destructive: true,
    engine: 'chat-prompt-live',
    id: 'prompt.live-mobile-viewport',
    prerequisites: ['The Chromium chat-prompt session'],
    profiles: ['chat-prompt'],
    title: 'The live mobile viewport keeps template creation and deletion reachable',
  },
  {
    cleanup: 'Asserts the visible template deletion and zero server residue itself.',
    destructive: true,
    engine: 'chat-prompt-live',
    id: 'prompt.cleanup-integrity',
    prerequisites: ['The shared per-browser prompt template'],
    profiles: ['chat-prompt'],
    title: 'The visible template deletion leaves zero run-bound prompt residue per browser',
  },
  {
    cleanup: 'No resources of its own; observational over all chat-prompt contexts.',
    destructive: false,
    engine: 'chat-prompt-live',
    id: 'chatprompt.console-network-clean',
    prerequisites: ['The shared chat-prompt browser sessions'],
    profiles: ['chat-prompt'],
    title: 'Zero unexplained console errors, failed requests, and 5xx responses outside the offline window',
  },
  collaborationScenario(
    'fault.revocation',
    'Live access revocation and hidden 404',
    '@revocation',
    ['fault-injection'],
    undefined,
    true,
  ),
  collaborationScenario(
    'fault.permission-downgrade',
    'Live edit-to-view downgrade',
    '@permission-downgrade',
    ['fault-injection'],
    undefined,
    true,
  ),
  collaborationScenario(
    'fault.lost-ack',
    'Lost durable acknowledgement reconciliation',
    '@reconciliation',
    ['fault-injection'],
    undefined,
    true,
  ),
  collaborationScenario(
    'fault.sidecar-outage',
    'Collaboration-sidecar outage, restart, and recovery',
    '@outage',
    ['fault-injection'],
    undefined,
    true,
  ),
  collaborationScenario(
    'fault.gateway-outage',
    'Public gateway outage and recovery',
    '@gateway-outage',
    ['fault-injection'],
    undefined,
    true,
  ),
  collaborationScenario(
    'fault.private-anchors',
    'Private anchor isolation during remote edits',
    '@private-anchors',
    ['fault-injection'],
  ),
  collaborationScenario(
    'system.detached-transfer',
    'Detached export and import',
    '@detached-transfer',
    ['system-smoke'],
  ),
  collaborationScenario(
    'fault.protocol-rejection',
    'Incompatible protocol and schema rejection',
    '@protocol-rejection',
    ['fault-injection'],
    undefined,
    true,
  ),
  loadScenario(
    'load-smoke.protocol',
    'Small authenticated Yjs protocol cohort',
    'load-smoke',
  ),
  loadScenario(
    'load-smoke.durability',
    'Small visibility and durable-ack sample',
    'load-smoke',
  ),
  loadScenario(
    'load-smoke.reconstruction',
    'Small observer reconstruction check',
    'load-smoke',
  ),
  loadScenario(
    'load-soak.identity-matrix',
    'Twenty-five distinct identities and ordered role cohorts',
    'load-soak',
  ),
  loadScenario(
    'load-soak.comments-and-navigation',
    'Fifty comment threads and repeated reader navigation',
    'load-soak',
  ),
  loadScenario(
    'load-soak.network-phases',
    'Normal, latency, bandwidth, packet-loss, and normalization phases',
    'load-soak',
    true,
  ),
  loadScenario(
    'load-soak.durability',
    'Phase-anchored visibility, durable acknowledgement, and reconstruction',
    'load-soak',
  ),
  loadScenario(
    'load-soak.feature-activity',
    'Staggered cost-bounded Research, Knowledge, and Agent activity',
    'load-soak',
  ),
  loadScenario(
    'load-soak.resource-recovery',
    'Sockets, memory, CPU, pools, and final recovery remain bounded',
    'load-soak',
  ),
  loadScenario(
    'load-ramp.ladder',
    'Resource-gated socket fan-out ladder with an honest controlled stop',
    'load-ramp',
  ),
  loadScenario(
    'load-ramp.integrity',
    'Exact reconstruction and zero lost or duplicate mutations at every reached rung',
    'load-ramp',
  ),
  loadScenario(
    'load-capacity.latency',
    'Fixed visible-update, durable-ack, and API latency budgets',
    'load-capacity',
  ),
  loadScenario(
    'load-capacity.rotation',
    'Connected and scheduled lease rotation',
    'load-capacity',
  ),
  loadScenario(
    'load-capacity.restart',
    'Ungraceful restart and observer reconstruction',
    'load-capacity',
    true,
  ),
  edgeScenario(
    'edge.static-spa-cache',
    'Static assets, hard asset 404s, and SPA cache behavior',
  ),
  edgeScenario(
    'edge.readiness-contract',
    'Ready and degraded dependency status relay across edge adapters',
  ),
  edgeScenario(
    'edge.http-streaming-cookies',
    'SSE streaming and duplicate Set-Cookie relay',
  ),
  edgeScenario(
    'edge.hop-by-hop-headers',
    'Request and response hop-by-hop header removal',
  ),
  edgeScenario(
    'edge.request-body-limit',
    'Header-independent request-body ceiling',
  ),
  edgeScenario(
    'edge.websocket-contract',
    'Binary WebSocket query and close behavior',
  ),
  edgeScenario(
    'edge.backend-recovery',
    'Backend outage and same-process recovery',
  ),
  edgeScenario(
    'edge.guest-security-and-redaction',
    'Normalized guest/share-link privacy and log redaction',
  ),
  edgeScenario(
    'edge.runtime-hardening',
    'Non-root, read-only, capability-dropped runtime',
  ),
] as const

validateScenarioInventory(SCENARIO_INVENTORY)

export function scenariosForProfile(
  profile: VerificationProfile,
): readonly ScenarioDefinition[] {
  return SCENARIO_INVENTORY.filter((scenario) => scenario.profiles.includes(profile))
}

export function scenarioIdsForProfile(profile: VerificationProfile): string[] {
  return scenariosForProfile(profile).map((scenario) => scenario.id)
}

export function uiScenarioForTestTitle(
  title: string,
): ScenarioDefinition | undefined {
  return SCENARIO_INVENTORY.find((scenario) => (
    scenario.engine === 'ui-fixture-playwright'
    && scenario.testTitle === title
  ))
}

export function collaborationScenarioForTags(
  profile: VerificationProfile,
  tags: readonly string[],
): ScenarioDefinition | undefined {
  return scenariosForProfile(profile).find((scenario) => (
    scenario.engine === 'collaboration-playwright'
    && scenario.selectorTag !== undefined
    && tags.includes(scenario.selectorTag)
  ))
}

export function requiredPlaywrightTags(
  profile: VerificationProfile,
  formFactor: 'desktop' | 'mobile',
  browser: CollaborationBrowserTarget['browser'] = 'chromium',
): string[] {
  return scenariosForProfile(profile)
    .filter((scenario) => (
      scenario.engine === 'collaboration-playwright'
      && scenario.selectorTag
      && (!scenario.formFactors || scenario.formFactors.includes(formFactor))
      && (!scenario.browsers || scenario.browsers.includes(browser))
    ))
    .map((scenario) => scenario.selectorTag!)
}

export function playwrightGrep(profile: VerificationProfile): string {
  const tags = new Set([
    ...requiredPlaywrightTags(profile, 'desktop'),
    ...requiredPlaywrightTags(profile, 'mobile'),
  ])
  if (tags.size === 0) {
    throw new Error(`Profile ${profile} has no collaboration Playwright scenarios.`)
  }
  return [...tags].join('|')
}

function uiScenario(
  id: string,
  title: string,
  testTitle: string,
  browsers?: readonly CollaborationBrowserTarget['browser'][],
): ScenarioDefinition {
  return {
    ...(browsers ? { browsers } : {}),
    cleanup: 'Playwright closes the isolated browser context and fixture server.',
    destructive: false,
    engine: 'ui-fixture-playwright',
    id,
    prerequisites: ['Local browser runtime'],
    profiles: ['ui-fixture'],
    testTitle,
    title,
  }
}

function edgeScenario(
  id: string,
  title: string,
): ScenarioDefinition {
  return {
    cleanup: 'The orchestrator removes exact run-labelled containers, network, and image tags, then verifies zero residual resources.',
    destructive: false,
    engine: 'web-edge-containers',
    id,
    prerequisites: [
      'Explicit Podman or Docker engine',
      'Local image build capability',
    ],
    profiles: ['edge-conformance'],
    title,
  }
}

function collaborationScenario(
  id: string,
  title: string,
  selectorTag: `@${string}`,
  profiles: readonly VerificationProfile[],
  formFactors: readonly ('desktop' | 'mobile')[] = ['desktop', 'mobile'],
  destructive = false,
  browsers?: readonly CollaborationBrowserTarget['browser'][],
): ScenarioDefinition {
  return {
    browsers,
    cleanup: destructive
      ? 'External fixture controls restore the affected capability; disposable documents are removed by fixture ownership.'
      : 'Disposable fixture documents remain externally owned and are cleaned by the fixture lifecycle.',
    destructive,
    engine: 'collaboration-playwright',
    formFactors,
    id,
    prerequisites: ['Version-2 collaboration fixture', 'Isolated owner and collaborator identities'],
    profiles,
    selectorTag,
    title,
  }
}

function loadScenario(
  id: string,
  title: string,
  profile: 'load-smoke' | 'load-soak' | 'load-ramp' | 'load-capacity',
  destructive = false,
): ScenarioDefinition {
  return {
    cleanup: 'The load engine closes every socket and stops the lease-rotation supervisor in finally.',
    destructive,
    engine: 'collaboration-load',
    id,
    prerequisites: ['Version-2 lease/session fixture'],
    profiles: [profile],
    title,
  }
}

function validateScenarioInventory(inventory: readonly ScenarioDefinition[]): void {
  const ids = new Set<string>()
  for (const scenario of inventory) {
    if (ids.has(scenario.id)) throw new Error(`Duplicate verification scenario: ${scenario.id}`)
    ids.add(scenario.id)
    if (scenario.profiles.length === 0) {
      throw new Error(`Verification scenario ${scenario.id} has no profile.`)
    }
    for (const profile of scenario.profiles) {
      if (!PROFILE_ENGINE_ORDER[profile].includes(scenario.engine)) {
        throw new Error(
          `Verification scenario ${scenario.id} uses engine ${scenario.engine} outside profile ${profile}.`,
        )
      }
    }
  }
  for (const [profile, engines] of Object.entries(PROFILE_ENGINE_ORDER) as Array<
    [VerificationProfile, readonly VerificationEngine[]]
  >) {
    const selected = inventory.filter((scenario) => scenario.profiles.includes(profile))
    if (selected.length === 0) throw new Error(`Verification profile ${profile} has no scenarios.`)
    for (const engine of engines) {
      if (!selected.some((scenario) => scenario.engine === engine)) {
        throw new Error(`Verification profile ${profile} has no scenario for engine ${engine}.`)
      }
    }
  }
}
