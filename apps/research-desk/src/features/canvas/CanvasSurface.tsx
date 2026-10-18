/**
 * THE canvas reading width. Every canvas view (report, plan, evidence,
 * run overview, task detail, diff, patch) centers its content in this
 * one measure so tab switches never jump between widths — `max-w-4xl`
 * matches the inline chat-answer block, keeping the same text at the
 * same measure inline and in canvas. Change it HERE, never per view.
 */
export const canvasSurfaceClass = 'mx-auto w-full max-w-4xl px-4 py-6 sm:px-6'
