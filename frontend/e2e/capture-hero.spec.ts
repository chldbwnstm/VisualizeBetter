/**
 * Hero screenshot capture (docs/screenshot-self-visualization.png).
 *
 * Not a test — a capture harness kept next to the E2E suite because it needs the
 * same thing the suite needs: a real `visualizebetter serve`, the built SPA, and a
 * genuine WebGL context. Driving a personal browser by hand produced a screenshot
 * that quietly aged (it showed pre-rename paths long after the rename), so the
 * shot is reproducible instead.
 *
 * Point it at a server whose graph already holds what you want photographed:
 *
 *   VISUALIZEBETTER_URL=http://127.0.0.1:8765 \
 *     npx playwright test e2e/capture-hero.spec.ts
 *
 * Skipped unless CAPTURE_HERO=1, so a normal suite run does not overwrite the
 * committed image.
 */
import { expect, test } from '@playwright/test'
import { callTool, connectMcp } from '../perf/mcpClient'

const LANG = process.env.CAPTURE_LANG === 'ko' ? 'ko' : 'en'
const OUT =
  LANG === 'ko'
    ? '../docs/screenshot-self-visualization.ko.png'
    : '../docs/screenshot-self-visualization.png'

test.skip(process.env.CAPTURE_HERO !== '1', 'set CAPTURE_HERO=1 to re-shoot the hero image')

test('capture the hero screenshot', async ({ page }) => {
  test.setTimeout(180_000)
  await page.setViewportSize({ width: 1600, height: 900 })
  await page.goto('/')

  // The UI language is per-user state. Each README shows its own language —
  // an English page carrying a Korean-UI screenshot was the original problem.
  await page.evaluate((lang) => {
    localStorage.setItem('vb.lang', lang)
  }, LANG)
  await page.reload()

  // Wait for real content rather than a fixed sleep: the overview only has
  // something worth photographing once the graph has arrived and cosmos has laid
  // it out.
  await expect(page.locator('canvas').first()).toBeVisible({ timeout: 60_000 })
  await page.waitForFunction(
    async () => (await (await fetch('/graph.json')).json()).nodes.length > 0,
    undefined,
    { timeout: 60_000 },
  )

  // Split view: the point of the product is the two views side by side, so a
  // hero shot of the overview alone undersells it.
  await connectMcp()
  const split = page.getByTestId('split-toggle').locator('input')
  if (!(await split.isChecked())) await split.check()

  // Focus the import hub through the same MCP tool an AI would call — that is
  // the interaction the image is supposed to show, and it fills the detail view
  // and the inspector with real content instead of an empty-state message.
  await callTool('focus_on', { node_id: 'visualizebetter/graph/core.py' })

  await page.waitForTimeout(8000) // force layout + fcose settle
  await page.screenshot({ path: OUT, fullPage: false })
  console.log(`wrote ${OUT}`)
})
