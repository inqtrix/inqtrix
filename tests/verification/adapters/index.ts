import type { VerificationAdapter } from '../adapter.ts'
import { agentDeskLiveAdapter } from './agent-desk-live.ts'
import { chatPromptLiveAdapter } from './chat-prompt-live.ts'
import { collaborationLoadAdapter } from './collaboration-load.ts'
import { collaborationPlaywrightAdapter } from './collaboration-playwright.ts'
import { editorSystemLiveAdapter } from './editor-system-live.ts'
import { ownerSetupLiveAdapter } from './owner-setup-live.ts'
import { uiFixtureAdapter } from './ui-fixture.ts'
import { webEdgeContainersAdapter } from './web-edge-containers.ts'

export const VERIFICATION_ADAPTERS: readonly VerificationAdapter[] = [
  uiFixtureAdapter,
  ownerSetupLiveAdapter,
  collaborationPlaywrightAdapter,
  editorSystemLiveAdapter,
  agentDeskLiveAdapter,
  chatPromptLiveAdapter,
  collaborationLoadAdapter,
  webEdgeContainersAdapter,
]
