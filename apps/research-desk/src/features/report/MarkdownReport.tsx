import { MarkdownRenderer } from '@/components/markdown/MarkdownRenderer'
import { AI_PRODUCER } from '@/lib/aiDisclosure'

type MarkdownReportProps = {
  markdown: string
}

/** The research report body. Always model-generated, so the AI markers are
 * unconditional here — unlike {@link MarkdownSelectionCopyMenu}, which also
 * wraps bodies that are not model output. */
export function MarkdownReport({ markdown }: MarkdownReportProps) {
  return (
    <div
      className="w-full min-w-0 max-w-full [overflow-wrap:anywhere]"
      data-ai-generated="true"
      data-ai-producer={AI_PRODUCER}
    >
      <MarkdownRenderer markdown={markdown} variant="report" />
    </div>
  )
}
