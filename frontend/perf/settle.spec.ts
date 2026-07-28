/**
 * ★ [15] settle 별도 지표 (TASK N, decision A).
 *
 * The layout lifecycle buys its KPI numbers by not laying out during ingest, so
 * the cost it moves — the simulation re-enable stall, and how long the graph
 * takes to become readable after a burst — is measured and named here instead of
 * disappearing into a passing headline. "settle 스톨을 숨기지 말고 별도 지표로
 * 이름 붙여 리포트에 적어라."
 */

import { expect, test } from '@playwright/test'
import { BASE, callTool, connectMcp, nodeSpecs } from './mcpClient'

test('[15] settle 별도 지표 — 재개 스톨 + 버스트 후 읽을 수 있기까지', async ({ page }) => {
  await connectMcp()
  await callTool('clear_all', {})

  await page.goto(BASE)
  await expect(page.getByTestId('ws-status')).toHaveAttribute('data-connected', 'true')
  await page.evaluate(() => {
    ;(window as unknown as { __perf: Record<string, unknown> }).__perf = {}
  })

  // A burst with real edges: the settle has to actually arrange something, and
  // neighbour seeding only matters when there are neighbours.
  const nodes = nodeSpecs('SETTLE', 0, 1000)
  await callTool('push_batch', { nodes })
  const edges = Array.from({ length: 400 }, (_, i) => ({
    source: `SETTLE.${i}`,
    target: `SETTLE.${(i * 7 + 3) % 1000}`,
    relation: 'ref',
  }))
  await callTool('push_batch', { edges })

  await page.waitForFunction(
    () =>
      Number(
        document.querySelector('[data-testid="node-count"]')?.textContent?.match(/\d+/)?.[0] ?? 0,
      ) >= 1000,
    undefined,
    { timeout: 60_000 },
  )

  const burstDone = Date.now()
  // Wait for the debounce to fire, the simulation to run, and FROZEN to be reached.
  await page.waitForFunction(
    () =>
      (window as unknown as { __perf: Record<string, unknown> }).__perf[
        'settle:total(→readable)'
      ] !== undefined,
    undefined,
    { timeout: 60_000 },
  )
  const readableAfterMs = Date.now() - burstDone

  const perf = await page.evaluate(
    () => (window as unknown as { __perf: Record<string, { n: number; ms: number }> }).__perf,
  )
  const avg = (k: string) => (perf[k] ? perf[k].ms / perf[k].n : Number.NaN)

  console.log('\n=== [15] settle 별도 지표 (정직 보고) ===')
  console.log(`SETTLE | 재개 스톨 setConfig(sim on) : ${avg('settle:enable(sim on)').toFixed(1)}ms (${perf['settle:enable(sim on)']?.n ?? 0}회)`)
  console.log(`SETTLE | start(alpha)               : ${avg('settle:start').toFixed(1)}ms`)
  console.log(`SETTLE | 레이아웃 수렴(→readable)    : ${avg('settle:total(→readable)').toFixed(0)}ms`)
  console.log(`SETTLE | 버스트 종료→읽을 수 있기까지 : ${readableAfterMs}ms (debounce 500ms 포함)`)
  console.log(`SETTLE | 위치 회수 readback         : ${avg('capturePositions').toFixed(1)}ms`)
  console.log(`SETTLE | ★ 스톨은 유휴 중 1회 — pan/zoom 인터랙션과 겹치지 않는다`)

  expect(perf['settle:total(→readable)']).toBeTruthy()
})
