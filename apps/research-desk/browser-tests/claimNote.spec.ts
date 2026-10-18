import { expect, test, type Locator, type Page } from '@playwright/test'

// The note of a claim mark stands where the marked statement is, as a
// comment does in a document editor: tapping or focusing the mark shows it
// right at the tapped line, never at the end of a long answer.
const FIXTURE = '/browser-tests/fixtures/agent-activity.html'
const GAP_LIMIT = 12

test.beforeEach(async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await page.setViewportSize({ height: 900, width: 1280 })
  await page.goto(FIXTURE)
  await page.getByRole('button', { exact: true, name: 'Antwort mit Marke' }).click()
})

function firstMark(page: Page): Locator {
  return page.locator('[data-claim-first]').first()
}

function card(page: Page): Locator {
  return page.getByTestId('agent-claim-note-card')
}

async function box(locator: Locator) {
  const found = await locator.boundingBox()
  expect(found).not.toBeNull()
  return found!
}

test('the note of a tapped mark stands right under the tapped line', async ({ page }) => {
  const mark = firstMark(page)
  const note = await mark.getAttribute('data-claim-note')
  await mark.click()
  await expect(card(page)).toBeVisible()
  await expect(card(page)).toHaveText(note!)
  const [line, shown] = [await box(mark), await box(card(page))]
  const gap = shown.y - (line.y + line.height)
  expect(gap).toBeGreaterThanOrEqual(0)
  expect(gap).toBeLessThanOrEqual(GAP_LIMIT)
  expect(shown.y + shown.height).toBeLessThanOrEqual(900)
  // Screen readers hear the note once, from the live region.
  await expect(card(page)).toHaveAttribute('aria-hidden', 'true')
  await expect(page.getByTestId('agent-claim-note')).toHaveText(note!)
})

test('Escape and a tap outside the answer close the note', async ({ page }) => {
  const mark = firstMark(page)
  await mark.click()
  await expect(card(page)).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(card(page)).toHaveCount(0)

  await mark.click()
  await expect(card(page)).toBeVisible()
  await page.getByRole('navigation', { name: 'Prüfszenarien' }).click({ position: { x: 4, y: 4 } })
  await expect(card(page)).toHaveCount(0)
})

test('the focused mark shows its note under it', async ({ page }) => {
  const mark = firstMark(page)
  await mark.focus()
  await expect(card(page)).toBeVisible()
  const gap = (await box(card(page))).y - ((await box(mark)).y + (await box(mark)).height)
  expect(gap).toBeGreaterThanOrEqual(0)
  expect(gap).toBeLessThanOrEqual(GAP_LIMIT)
})

test('a mark at the bottom of the window shows its note above it', async ({ page }) => {
  const mark = firstMark(page)
  await mark.scrollIntoViewIfNeeded()
  const line = await box(mark)
  // The window ends just under the mark: no room below for the note.
  await page.setViewportSize({ height: Math.ceil(line.y + line.height + 20), width: 1280 })
  await mark.click()
  await expect(card(page)).toBeVisible()
  const [moved, shown] = [await box(mark), await box(card(page))]
  const gap = moved.y - (shown.y + shown.height)
  expect(gap).toBeGreaterThanOrEqual(0)
  expect(gap).toBeLessThanOrEqual(GAP_LIMIT)
})

test('a mark over several lines shows its note under the line that was tapped', async ({ page }) => {
  await page.setViewportSize({ height: 800, width: 360 })
  const mark = firstMark(page)
  // In the middle of the window, so the note has room under the line.
  await mark.evaluate((element) => element.scrollIntoView({ block: 'center' }))
  const first = await mark.evaluate((element) => {
    const rects = element.getClientRects()
    return { count: rects.length, height: rects[0].height, x: rects[0].x, y: rects[0].y }
  })
  expect(first.count).toBeGreaterThan(1)
  await page.mouse.click(first.x + 4, first.y + first.height / 2)
  await expect(card(page)).toBeVisible()
  await expect(card(page)).toHaveAttribute('data-side', 'below')
  const gap = (await box(card(page))).y - (first.y + first.height)
  expect(gap).toBeGreaterThanOrEqual(0)
  expect(gap).toBeLessThanOrEqual(GAP_LIMIT)
})

test('the note keeps its room to both sides of a narrow window', async ({ page }) => {
  await page.setViewportSize({ height: 800, width: 360 })
  const mark = firstMark(page)
  await mark.evaluate((element) => element.scrollIntoView({ block: 'center' }))
  await mark.click()
  await expect(card(page)).toBeVisible()
  const shown = await box(card(page))
  expect(shown.x).toBeGreaterThanOrEqual(8)
  expect(shown.x + shown.width).toBeLessThanOrEqual(360 - 8)
})

test('only one note stands when two answers carry marks', async ({ page }) => {
  await page.getByRole('button', { exact: true, name: 'Zweite Antwort mit Marke' }).click()
  const marks = page.locator('[data-claim-first]')
  await expect(marks).toHaveCount(2)
  await marks.nth(0).click()
  await expect(card(page)).toHaveCount(1)
  await marks.nth(1).scrollIntoViewIfNeeded()
  await marks.nth(1).click()
  await expect(card(page)).toHaveCount(1)
  const [line, shown] = [await box(marks.nth(1)), await box(card(page))]
  const gap = shown.y - (line.y + line.height)
  expect(gap).toBeGreaterThanOrEqual(0)
  expect(gap).toBeLessThanOrEqual(GAP_LIMIT)
})
