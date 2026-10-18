import { appMotion } from '@/motion/transitions'
import { AnimatePresence, motion } from 'motion/react'
import { TextImproveReviewPanel } from './TextImproveReviewPanel'
import type { TextImproveReviewLabels, TextImprovementProposal } from './types'

export function TextImproveFieldLayer({
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
          className="absolute inset-0 z-20 origin-top-right rounded-md"
          exit={reduceMotion ? { opacity: 0 } : { filter: 'blur(2px)', opacity: 0, scale: 0.99, y: 4 }}
          initial={reduceMotion ? { opacity: 0 } : { filter: 'blur(3px)', opacity: 0, scale: 0.99, y: 6 }}
          transition={appMotion.panel}
        >
          <TextImproveReviewPanel
            className="border-brand/30 !bg-background !backdrop-blur-none shadow-[0_12px_34px_var(--shadow-soft)]"
            density="comfortable"
            fill
            labels={labels}
            onAccept={onAccept}
            onReject={onReject}
            proposal={proposal}
            reduceMotion={reduceMotion}
          />
        </motion.div>
      )}
    </AnimatePresence>
  )
}
