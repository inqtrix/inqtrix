import { appMotion } from '@/motion/transitions'
import { AnimatePresence, motion } from 'motion/react'
import { TextImproveReviewPanel } from './TextImproveReviewPanel'
import type { TextImproveReviewLabels, TextImprovementProposal } from './types'

export function TextImproveFloatingLayer({
  labels,
  onAccept,
  onReject,
  proposal,
  reduceMotion,
}: {
  labels: TextImproveReviewLabels
  onAccept: (text: string) => void
  onReject: () => void
  proposal: TextImprovementProposal | null
  reduceMotion: boolean | null
}) {
  return (
    <AnimatePresence>
      {proposal && (
        <motion.div
          animate={{ filter: 'blur(0px)', opacity: 1, scale: 1, y: 0 }}
          className="pointer-events-none absolute inset-x-0 bottom-[calc(100%+0.75rem)] z-40 origin-bottom-right"
          exit={reduceMotion ? { opacity: 0 } : { filter: 'blur(2px)', opacity: 0, scale: 0.985, y: 8 }}
          initial={reduceMotion ? { opacity: 0 } : { filter: 'blur(3px)', opacity: 0, scale: 0.985, y: 12 }}
          transition={appMotion.panel}
        >
          <div className="pointer-events-auto">
            <TextImproveReviewPanel
              className="bg-card/97"
              contentClassName="max-h-[min(20rem,calc(100vh-15rem))] overflow-y-auto"
              density="compact"
              labels={labels}
              onAccept={onAccept}
              onReject={onReject}
              proposal={proposal}
              reduceMotion={reduceMotion}
            />
          </div>
        </motion.div>
      )}
    </AnimatePresence>
  )
}
