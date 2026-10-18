import { expect, test } from '@playwright/test'

test('sub-threshold cold work never paints a fallback', async ({ page }) => {
  await page.goto('/browser-tests/fixtures/structural-loading.html?readyAfterMs=750')
  const region = page.locator('[data-structural-region]')
  await expect(region).toHaveAttribute('data-structural-state', 'ready', { timeout: 5_000 })
  await expect(page.locator('[data-structural-fallback]')).toHaveCount(0)
  await expect(page.locator('[data-fixture-target]')).toBeVisible()
})

test('a painted fallback observes its minimum dwell and single reveal', async ({ page }) => {
  await page.goto('/browser-tests/fixtures/structural-loading.html?readyAfterMs=900')
  const fallback = page.locator('[data-structural-fallback]')
  await expect(fallback).toBeVisible({ timeout: 2_000 })
  const appearedAt = await page.evaluate(() => performance.now())
  await expect(fallback).toHaveCount(0, { timeout: 3_000 })
  const disappearedAt = await page.evaluate(() => performance.now())

  expect(disappearedAt - appearedAt).toBeGreaterThanOrEqual(280)
  await expect(page.locator('[data-fixture-target]')).toBeVisible()
  await expect(page.locator('[data-structural-region]')).toHaveAttribute(
    'data-structural-state',
    'ready',
  )
})

test('geometry blockers delay publication without introducing another fallback', async ({ page }) => {
  await page.goto(
    '/browser-tests/fixtures/structural-loading.html?readyAfterMs=100&blockerAfterMs=900',
  )
  const region = page.locator('[data-structural-region]')
  const fallback = page.locator('[data-structural-fallback]')
  await expect(fallback).toBeVisible({ timeout: 2_000 })
  await expect(region).toHaveAttribute('data-structural-blockers', '1')
  await expect(fallback).toHaveCount(0, { timeout: 3_000 })
  await expect(region).toHaveAttribute('data-structural-blockers', '0')
  await expect(page.locator('[data-fixture-target]')).toHaveCount(1)
})

test('reduced motion disables shimmer and the fallback exit animation', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await page.goto('/browser-tests/fixtures/structural-loading.html?readyAfterMs=900')
  const fallback = page.locator('[data-structural-fallback]')
  await expect(fallback).toBeVisible({ timeout: 2_000 })
  const animationNames = await fallback.locator('.animate-pulse').evaluateAll((elements) => (
    elements.map((element) => getComputedStyle(element).animationName)
  ))
  expect(animationNames.every((name) => name === '' || name === 'none')).toBe(true)
  await expect(fallback).toHaveCount(0, { timeout: 3_000 })
})
