import {
  ResizablePanel,
  ResizablePanelGroup,
} from '@/components/ui/resizable'
import { useCallback, useEffect, useState } from 'react'
import {
  AnimatedPanelBody,
  AnimatedResizableHandle,
} from '@/components/ui/animated-panel'
import { useAnimatedResizablePanelCollapse } from '@/components/ui/animated-panel-motion'
import { ResponsiveSidePanel } from '@/components/ui/responsive-side-panel'
import type { CreateResearchRunRequest } from '@/features/researchRuns/types'
import { ReportPanel } from '@/features/report/ReportPanel'
import type { ResearchRunRecord } from '@/features/project/types'
import { useLocale } from '@/i18n/LocaleProvider'
import type { JobFilter, ResearchJob } from '../types'
import type { ResearchSubmissionOutcome } from '../researchSubmission'
import { ResearchRunColumn } from './ResearchRunColumn'

const RESEARCH_REPORT_PANEL_ID = 'research-report-panel'
const RESEARCH_RUN_PANEL_ID = 'research-run-panel'

type ResearchWorkspaceProps = {
  /** Server run listing still in flight; forwarded to the run column. */
  runsLoading?: boolean
  activeFilter: JobFilter
  allJobs: ResearchJob[]
  authenticatedUserId: string | null
  cancelErrorByRunId: Record<string, string>
  cancelSubmittingRunIds: ReadonlySet<string>
  expandedJobId: string | null
  isComposerVisible: boolean
  isDesktop: boolean
  isReportVisible: boolean
  /** Disable run submission (composer send + start-screen suggestions)
   * while the auth session is still resolving, instead of a silent no-op. */
  isSubmitDisabled: boolean
  jobs: ResearchJob[]
  onActiveFilterChange: (filter: JobFilter) => void
  onCancelJob: (jobId: string) => void
  onAuthenticationRequired: () => void
  onComposerSubmit: (request: CreateResearchRunRequest) => Promise<ResearchSubmissionOutcome>
  onComposerVisibleChange: (isComposerVisible: boolean) => void
  onResearchQuestionChange: (question: string) => void
  researchQuestion: string
  onDeleteJob: (jobId: string) => void
  onReportPanelSizeChange: (size: number) => void
  onReportVisibleChange: (isVisible: boolean) => void
  onSelectJob: (jobId: string) => void
  onSetReportAutocomplete?: (runId: string, includeInAutocomplete: boolean) => void
  onShareJob?: (jobId: string) => void
  onToggleJob: (jobId: string) => void
  /** Load a finished run's report again after its result failed to load. */
  onRetryReportResult?: (runId: string) => void
  onUseReportInChat: (runId: string) => void
  reduceMotion: boolean | null
  reportPanelSize: number
  selectedJobId: string | null
  selectedRun: ResearchRunRecord | null
  selectedStack: string
  shareCountByRunId?: Record<string, number>
}

export function ResearchWorkspace({
  runsLoading = false,
  activeFilter,
  allJobs,
  authenticatedUserId,
  cancelErrorByRunId,
  cancelSubmittingRunIds,
  expandedJobId,
  isComposerVisible,
  isDesktop,
  isReportVisible,
  isSubmitDisabled,
  jobs,
  onActiveFilterChange,
  onAuthenticationRequired,
  onCancelJob,
  onComposerSubmit,
  onComposerVisibleChange,
  onResearchQuestionChange,
  researchQuestion,
  onDeleteJob,
  onReportPanelSizeChange,
  onReportVisibleChange,
  onSelectJob,
  onSetReportAutocomplete,
  onShareJob,
  onToggleJob,
  onRetryReportResult,
  onUseReportInChat,
  reduceMotion,
  reportPanelSize,
  selectedJobId,
  selectedRun,
  selectedStack,
  shareCountByRunId,
}: ResearchWorkspaceProps) {
  const { t } = useLocale()
  const [isMobileReportOpen, setIsMobileReportOpen] = useState(false)
  const reportPanelMotion = useAnimatedResizablePanelCollapse({
    expandedSize: reportPanelSize,
    expanded: isReportVisible,
    reduceMotion,
  })
  const reportPanelLayout = {
    [RESEARCH_REPORT_PANEL_ID]: isReportVisible ? reportPanelSize : 0,
    [RESEARCH_RUN_PANEL_ID]: isReportVisible ? 100 - reportPanelSize : 100,
  }

  useEffect(() => {
    if (isDesktop) setIsMobileReportOpen(false)
  }, [isDesktop])

  const controlReportVisible = isDesktop ? isReportVisible : isMobileReportOpen
  const handleReportVisibleChange = useCallback((isVisible: boolean) => {
    if (isDesktop) {
      onReportVisibleChange(isVisible)
      return
    }
    setIsMobileReportOpen(isVisible)
  }, [isDesktop, onReportVisibleChange])
  const handleDesktopReportHide = useCallback(() => {
    onReportVisibleChange(false)
  }, [onReportVisibleChange])
  const handleMobileReportHide = useCallback(() => {
    setIsMobileReportOpen(false)
  }, [])

  const runColumn = (
    <ResearchRunColumn
      runsLoading={runsLoading}
      activeFilter={activeFilter}
      allJobs={allJobs}
      authenticatedUserId={authenticatedUserId}
      cancelErrorByRunId={cancelErrorByRunId}
      cancelSubmittingRunIds={cancelSubmittingRunIds}
      expandedJobId={expandedJobId}
      isComposerVisible={isComposerVisible}
      isReportVisible={controlReportVisible}
      isSubmitDisabled={isSubmitDisabled}
      jobs={jobs}
      onActiveFilterChange={onActiveFilterChange}
      onAuthenticationRequired={onAuthenticationRequired}
      onCancelJob={onCancelJob}
      onComposerSubmit={onComposerSubmit}
      onComposerVisibleChange={onComposerVisibleChange}
      onReportVisibleChange={handleReportVisibleChange}
      reportPanelId={RESEARCH_REPORT_PANEL_ID}
      onResearchQuestionChange={onResearchQuestionChange}
      researchQuestion={researchQuestion}
      onDeleteJob={onDeleteJob}
      onSelectJob={onSelectJob}
      onShareJob={onShareJob}
      onToggleJob={onToggleJob}
      reduceMotion={reduceMotion}
      selectedJobId={selectedJobId}
      selectedStack={selectedStack}
      shareCountByRunId={shareCountByRunId}
    />
  )

  return (
    <div className="relative flex h-full min-h-0 w-full flex-1 overflow-hidden">
      <ResizablePanelGroup
        className="min-h-0 w-full overflow-hidden"
        defaultLayout={reportPanelLayout}
        elementRef={reportPanelMotion.groupRef}
        onLayoutChanged={(layout) => {
          const size = layout[RESEARCH_REPORT_PANEL_ID]
          if (
            isReportVisible
            && !reportPanelMotion.isProgrammaticLayoutChange()
            && Number.isFinite(size)
            && size > 0
          ) {
            onReportPanelSizeChange(size)
          }
        }}
        orientation="horizontal"
      >
        <ResizablePanel
          className="min-h-0 min-w-0 overflow-hidden"
          defaultSize={reportPanelLayout[RESEARCH_RUN_PANEL_ID]}
          id={RESEARCH_RUN_PANEL_ID}
          // Identity anchor — must stay unconditional so the run column DOM
          // survives the desktop/mobile flip and the report toggle.
          key={RESEARCH_RUN_PANEL_ID}
          maxSize={isReportVisible ? '74%' : '100%'}
          minSize="42%"
        >
          {runColumn}
        </ResizablePanel>
        {isDesktop && (
          <>
            <AnimatedResizableHandle
              aria-label="Resize report panel"
              expanded={isReportVisible}
            />
            <ResizablePanel
              className="min-h-0 min-w-0 overflow-hidden"
              collapsedSize="0%"
              collapsible
              defaultSize={reportPanelLayout[RESEARCH_REPORT_PANEL_ID]}
              id={RESEARCH_REPORT_PANEL_ID}
              maxSize="58%"
              minSize={isReportVisible ? '26%' : '0%'}
              panelRef={reportPanelMotion.panelRef}
            >
              <AnimatedPanelBody expanded={isReportVisible} side="right">
                <ReportPanel
                  onHide={handleDesktopReportHide}
                  onSetReportAutocomplete={onSetReportAutocomplete}
                  onRetryResult={onRetryReportResult}
                  onUseReportInChat={onUseReportInChat}
                  selectedRun={selectedRun}
                />
              </AnimatedPanelBody>
            </ResizablePanel>
          </>
        )}
      </ResizablePanelGroup>
      {!isDesktop && (
        <ResponsiveSidePanel
          closeLabel={t.report.hide}
          controlsId={RESEARCH_REPORT_PANEL_ID}
          onOpenChange={setIsMobileReportOpen}
          open={isMobileReportOpen}
          showHeader={false}
          side="right"
          title={t.report.title}
        >
          <ReportPanel
            onHide={handleMobileReportHide}
            onSetReportAutocomplete={onSetReportAutocomplete}
            onRetryResult={onRetryReportResult}
            onUseReportInChat={onUseReportInChat}
            selectedRun={selectedRun}
          />
        </ResponsiveSidePanel>
      )}
    </div>
  )
}
