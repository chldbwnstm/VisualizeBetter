/**
 * M3c 스팟체크 — 100K 에서 스크럽이 값싼(오버레이) 경로인지 실측. 구조경로 미진입.
 *
 * The store-level guard proves setTemporalCutoff does not bump structureSeq; this
 * confirms the consequence at scale: moving the cutoff on a 100K graph pays a
 * filter-dim-level overlay (recolour + link re-filter), never the structural
 * rebuild/settle M3a/M3b removed.
 */

import { expect, test } from '@playwright/test'
import { BASE, callTool, connectMcp } from './mcpClient'

const NODE_TARGET = 100_000

test('★ 100K 스크럽 = 오버레이 경로 (구조 rebuild/settle 미진입)', async ({ page }) => {
  test.setTimeout(600_000)
  await connectMcp()
  await callTool('clear_all', {})
  await callTool('import_from_file', { path: 'perf100k.json' })
  await page.goto(BASE)
  await expect(page.getByTestId('ws-status')).toHaveAttribute('data-connected', 'true')
  await expect(page.getByTestId('node-count')).toHaveText(`nodes ${NODE_TARGET}`, { timeout: 300_000 })
  await page.waitForTimeout(8000) // let the initial settle finish

  // The scrubber renders once the graph spans time (100K imported over ~seconds).
  await expect(page.getByTestId('temporal-slider')).toBeVisible()

  // Arm the harness counters AFTER the settle, then move the cutoff to the middle.
  await page.evaluate(() => { (window as unknown as { __perf: Record<string, unknown> }).__perf = {} })
  const range = await page.$eval('[data-testid="temporal-slider"]', (el) => {
    const input = el as HTMLInputElement
    return { min: Number(input.min), max: Number(input.max) }
  })
  const mid = Math.round((range.min + range.max) / 2)
  const t0 = Date.now()
  await page.$eval(
    '[data-testid="temporal-slider"]',
    (el, v) => {
      const input = el as HTMLInputElement
      input.value = String(v)
      input.dispatchEvent(new Event('input', { bubbles: true })) // React onChange
    },
    mid,
  )
  await page.waitForTimeout(300) // let the overlay effect run
  const scrubMs = Date.now() - t0

  const perf = await page.evaluate(
    () => (window as unknown as { __perf: Record<string, { n: number; ms: number }> }).__perf,
  )
  const has = (k: string) => perf[k] !== undefined
  console.log(`[M3c] 100K 스크럽 wall ${scrubMs}ms | buckets: ${Object.keys(perf).join(', ')}`)

  // ★ overlay path, not structural: the scrub recoloured (overlay:applyHighlight)
  // but never rebuilt (effect:rebuild) or re-seeded/settled.
  expect(has('overlay:applyHighlight')).toBe(true)
  expect(has('effect:rebuild')).toBe(false)
  expect(has('effect:applyDelta')).toBe(false)
  expect(has('settle:enable(sim on)')).toBe(false)
  // and it is cheap — a filter-dim-level overlay, well under a settle.
  expect(scrubMs).toBeLessThan(1500)
})
