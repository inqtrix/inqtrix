import '@fontsource-variable/inter/index.css'
import '@/styles/globals.css'

import { StrictMode, useState } from 'react'
import { createRoot } from 'react-dom/client'

import { AppProviders } from '@/app/AppProviders'
import { chatContextRefKey, type FileMentionOption } from '@/features/project/selectors'
import type { ChatContextReferenceRecord } from '@/features/project/types'

import ChatWorkspace from '../ChatWorkspace'

// The real chat workspace with its composer, wired the way the research desk
// wires it: the session draft lives in this page's state and survives the
// workspace being hidden and shown again (the desk unmounts it on a view
// switch), and Send shows the message and the references the request
// carries. Both files are in the user's library. Text improvement is on; a
// test answers its request.
function libraryFile(fileId: string, label: string): FileMentionOption {
  return { fileId, label, pageCount: 1, sizeBytes: 1024, title: label }
}

const fileOptions = [
  libraryFile('file-private', 'gehaltsliste-2026'),
  libraryFile('file-own', 'quartalsbericht'),
]

const noOp = () => undefined

type SentMessage = { content: string; refs: ChatContextReferenceRecord[] }

function ChatComposerFixture() {
  const [chatDraft, setChatDraft] = useState('')
  const [pillRefs, setPillRefs] = useState<ChatContextReferenceRecord[]>([])
  const [sent, setSent] = useState<SentMessage | null>(null)
  const [visible, setVisible] = useState(true)
  return (
    <main className="flex h-screen flex-col gap-2 p-4">
      <div className="flex items-center gap-3">
        <button className="rounded border px-3 py-1" onClick={() => setVisible((value) => !value)} type="button">
          {visible ? 'Chat ausblenden' : 'Chat einblenden'}
        </button>
        <output data-testid="sent">{sent?.content ?? ''}</output>
        <output data-testid="sent-refs">{sent ? JSON.stringify(sent.refs) : ''}</output>
      </div>
      <div className="min-h-0 flex-1">
        {visible ? (
          <ChatWorkspace
            activeAssistantMessageId={null}
            attachmentBudgetNotice={null}
            chainingEnabled={false}
            chatContextBase={{ conversation: 0, documents: 0, reports: 0, rules: 0 }}
            chatContextCapacity={{ contextWindowTokens: null, reservedOutputTokens: 0 }}
            chatDraft={chatDraft}
            chatHistorySections={[]}
            chatModelOptions={[]}
            chatModelOptionsStatus="missing"
            defaultChatModel={null}
            fileGroupOptions={[]}
            fileOptions={fileOptions}
            historyPanelSize={24}
            isDesktop
            isHistoryVisible={false}
            isIncognito={false}
            isHistoryPending={false}
            isMessagesLoading={false}
            isSending={false}
            knowledgeIndexOptions={null}
            messagesLoadError={null}
            onAdoptVisibleOrder={noOp}
            onAnswerLastUserMessage={noOp}
            onAttachContext={noOp}
            onAttachFiles={noOp}
            onBranchFromMessage={noOp}
            onChainingEnabledChange={noOp}
            onChangeSortMode={noOp}
            onChatDraftChange={setChatDraft}
            onClearThread={noOp}
            onCreateThread={noOp}
            onCreateThreadGroup={noOp}
            onDeleteMessages={noOp}
            onDeleteThread={noOp}
            onDeleteThreadGroup={noOp}
            onEditMessage={noOp}
            onHistoryPanelSizeChange={noOp}
            onHistoryVisibleChange={noOp}
            onIncognitoChange={noOp}
            onMoveThreadGroup={noOp}
            onMoveThreadToGroup={noOp}
            onOpenPromptLibrary={noOp}
            onPillRefsChange={setPillRefs}
            onRemoveContext={noOp}
            onRenameThread={noOp}
            onRenameThreadGroup={noOp}
            onReorderContext={noOp}
            onRetryAssistantMessage={noOp}
            onRetryAttachment={noOp}
            onRetryMessagesLoad={noOp}
            onSelectedChatEffortChange={noOp}
            onSelectedChatModelChange={noOp}
            onSelectedKnowledgeIndexIdsChange={noOp}
            onSelectedModelTierChange={noOp}
            onSelectThread={noOp}
            onSendMessage={async (content, refs) => {
              setSent({ content, refs: refs ?? [] })
              return true
            }}
            onStopGenerating={noOp}
            onStreamingEnabledChange={noOp}
            onTogglePinnedThread={noOp}
            pendingChips={[]}
            pendingReorderKeys={[]}
            pillKeys={pillRefs.map(chatContextRefKey)}
            pinnedThreadIds={[]}
            reduceMotion
            reportOptions={[]}
            requestError={null}
            requestNotice={null}
            ruleOptions={[]}
            runningThreadIds={new Set()}
            selectedChatEffort={null}
            selectedChatModel={null}
            selectedKnowledgeIndexIds={[]}
            selectedModelTier={null}
            selectedThreadId={null}
            sortMode="recent"
            streamingEnabled
            temporaryThread={null}
            textImprovement={{ enabled: true, workspaceId: 'chat-composer-fixture' }}
            threads={[]}
          />
        ) : null}
      </div>
    </main>
  )
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <AppProviders initialLocale="de">
      <ChatComposerFixture />
    </AppProviders>
  </StrictMode>,
)
