import { expect, type Dialog, type Page } from '@playwright/test'

/**
 * Leaves a page (a reload or a navigation) and accepts the browser's leave
 * question when one comes.
 *
 * A collaboration page asks before it unloads only while it holds local
 * changes that it could not send at that moment. Playwright accepts a leave
 * question that no handler takes, so without this helper an unexpected
 * question would pass unnoticed; every navigation away from an editor goes
 * through here. `expectQuestion` states whether the page's state at this
 * point should make it ask; any other outcome fails with the collaboration
 * status the page showed before it left, which is what a user would have
 * seen.
 */
export async function leavePageAcceptingQuestion(
  page: Page,
  leave: () => Promise<unknown>,
  { expectQuestion }: { expectQuestion: boolean },
): Promise<void> {
  const shownBefore = await collaborationStatusShown(page)
  const questions: string[] = []
  const accept = (dialog: Dialog): void => {
    questions.push(dialog.type())
    void dialog.accept().catch(() => undefined)
  }
  page.on('dialog', accept)
  try {
    await leave()
  } finally {
    page.off('dialog', accept)
  }
  expect(
    questions,
    `leave questions of a page whose collaboration status showed: ${shownBefore}`,
  ).toEqual(expectQuestion ? ['beforeunload'] : [])
}

/**
 * Reloads a page through leavePageAcceptingQuestion. The page starts the
 * reload itself: Playwright's own reload in Firefox runs none of the page's
 * beforeunload handlers and leaves its sockets open until pagehide, so
 * neither a leave question nor what an engine does to the page's sockets at
 * the start of a navigation could be observed there. A reload the page
 * starts runs the handlers in every engine.
 */
export function reloadAcceptingLeaveQuestion(
  page: Page,
  options: {
    expectQuestion: boolean
    waitUntil?: 'domcontentloaded' | 'load'
  },
): Promise<void> {
  return leavePageAcceptingQuestion(
    page,
    async () => {
      const loaded = options.waitUntil === 'domcontentloaded'
        ? page.waitForEvent('domcontentloaded')
        : page.waitForEvent('load')
      // Started from a timer, so the evaluation returns before the page
      // it runs in goes away.
      await page.evaluate(() => {
        window.setTimeout(() => window.location.reload(), 0)
      })
      await loaded
    },
    { expectQuestion: options.expectQuestion },
  )
}

/** Every collaboration status the page shows: its kind and its label. */
async function collaborationStatusShown(page: Page): Promise<string> {
  const shown = await page.evaluate(() => (
    [...document.querySelectorAll('[data-editor-status-kind]')].map((status) => {
      const label = status.querySelector('[data-editor-status-label]')?.textContent?.trim() ?? ''
      return `${status.getAttribute('data-editor-status-kind') ?? ''} (${label})`
    })
  )).catch(() => [])
  return shown.length > 0 ? shown.join(', ') : 'none'
}
