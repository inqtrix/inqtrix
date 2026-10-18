import { StrictMode, useEffect } from 'react'
import { createRoot } from 'react-dom/client'

import { AppProviders } from '@/app/AppProviders'
import { MarkdownRenderer, type MarkdownRendererVariant } from '@/components/markdown/MarkdownRenderer'
import renderingStandard from '@/features/project/demoContent/chat-history/chat-rendering-standard.md?raw'
import { parseChatThread } from '@/features/project/markdown'
import { useTheme, type ContrastMode, type ThemeMode, type ThemePreset } from '@/theme/ThemeProvider'
import '@/styles/globals.css'
import 'katex/dist/katex.min.css'

import repositoryReadme from '../../../../../../README.md?raw'
import { readmeArchitectureMarkdown } from './readmeDocument'

/**
 * The rendering sample set — the demo chat "Darstellungsstandard" — under
 * the real renderer, theme and stylesheet, driven by URL parameters:
 * `variant=chat|report`, `theme=light|dark`, `contrast=standard|high`,
 * `preset=petrol|standard|slate|graphite|sage`, and `extra=directive` to append
 * one diagram that tries to restyle itself through an init directive.
 * `sample=readme-architecture` renders the actual repository architecture.
 */
const parameters = new URLSearchParams(window.location.search)
const isReadme = parameters.get('sample') === 'readme-architecture'
const variant: MarkdownRendererVariant = parameters.get('variant') === 'report' ? 'report' : 'chat'
const theme: ThemeMode = parameters.get('theme') === 'dark' ? 'dark' : 'light'
const contrast: ContrastMode = parameters.get('contrast') === 'high' ? 'high' : 'standard'
const presetParameter = parameters.get('preset')
const preset: ThemePreset = presetParameter === 'petrol' || presetParameter === 'slate' || presetParameter === 'graphite' || presetParameter === 'sage'
  ? presetParameter
  : 'standard'

const DIRECTIVE_DIAGRAM = [
  '',
  '',
  '```mermaid',
  '%%{init: {"theme": "forest", "themeVariables": {"background": "#ffffff", "primaryColor": "#ff0000"}}}%%',
  'flowchart LR',
  '  A["Direktive"] --> B["ignoriert"]',
  '```',
  '',
].join('\n')

const thread = parseChatThread(renderingStandard)
const assistant = thread.messages.find((message) => message.role === 'assistant')
if (!assistant) throw new Error('The rendering standard thread carries no assistant message.')
const sampleMarkdown = isReadme ? readmeArchitectureMarkdown(repositoryReadme) : assistant.contentMarkdown
const markdown = parameters.get('extra') === 'directive'
  ? `${sampleMarkdown}${DIRECTIVE_DIAGRAM}`
  : sampleMarkdown

function ApplyTheme() {
  const { setContrastMode, setPreset, setTheme } = useTheme()
  useEffect(() => {
    setTheme(theme)
    setPreset(preset)
    setContrastMode(contrast)
  }, [setContrastMode, setPreset, setTheme])
  return null
}

function MarkdownRenderingFixture() {
  return (
    <main
      className="min-h-screen bg-background p-6 text-foreground"
      data-fixture-variant={variant}
      data-fixture-sample={isReadme ? 'readme-architecture' : 'rendering-standard'}
    >
      <ApplyTheme />
      {/* The same wrappers the product uses: the chat bubble column and the
          report body. Neither sets the reading-scale class — the renderer does. */}
      <div
        className={variant === 'chat'
          ? 'mx-auto max-w-4xl text-sm leading-snug text-foreground'
          : 'mx-auto w-full min-w-0 max-w-3xl [overflow-wrap:anywhere]'}
        data-fixture-body=""
      >
        {isReadme && <h1 className="t-title mb-6">README architecture</h1>}
        <MarkdownRenderer markdown={markdown} variant={variant} />
      </div>
    </main>
  )
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <AppProviders>
      <MarkdownRenderingFixture />
    </AppProviders>
  </StrictMode>,
)
