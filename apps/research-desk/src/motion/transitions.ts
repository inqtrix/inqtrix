export const appMotion = {
  card: {
    duration: 0.22,
    ease: [0.22, 1, 0.36, 1],
  },
  composer: {
    duration: 0.16,
    ease: [0.22, 1, 0.36, 1],
  },
  list: {
    duration: 0.18,
    ease: [0.22, 1, 0.36, 1],
  },
  panel: {
    duration: 0.26,
    ease: [0.22, 1, 0.36, 1],
  },
  /** The skeleton veil's release: a staged region reveals its settled
   * content by fading the covering silhouette out — deliberately the fastest
   * curve in the vocabulary, because at this moment the content is already
   * final and every extra millisecond of veil reads as lag, not polish. */
  reveal: {
    duration: 0.15,
    ease: [0.22, 1, 0.36, 1],
  },
  /** Page-in-page push into a detail layer: the incoming layer moves from
   * its navigation origin while the covered list recedes without unmounting. */
  push: {
    duration: 0.3,
    ease: [0.22, 1, 0.36, 1],
  },
  /** Return leg of the push: slightly faster so dismissal follows intent. */
  pushExit: {
    duration: 0.25,
    ease: [0.22, 1, 0.36, 1],
  },
} as const
