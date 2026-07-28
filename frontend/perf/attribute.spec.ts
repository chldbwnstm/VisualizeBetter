/**
 * ★ TASK N (1) 계측 — 155ms 의 지배요인을 실측으로 확정한다.
 *
 * TASK M narrowed the miss to a fixed ~155ms client-side cost per flush,
 * size-independent, and listed three candidates. This attributes the time to one
 * of them instead of picking the plausible-looking one. My own TASK M warning:
 * "위 중 무엇이 155ms 를 지배하는지 먼저 계측으로 확정 후 착수 — 추측으로 고치지 말 것."
 */

import { test } from '@playwright/test'
import { BASE, callTool, connectMcp, nodeSpecs } from './mcpClient'

test('계측: flush 경로 155ms 의 지배요인', async ({ page }) => {
  const total = Number(process.env.ATTR_NODES ?? 1000)
  await connectMcp()
  await callTool('clear_all', {})
  for (let from = 0; from < total; from += 1000) {
    await callTool('push_batch', { nodes: nodeSpecs('ATTR', from, Math.min(1000, total - from)) })
  }

  await page.goto(BASE)
  const renderer = await page.evaluate(() => {
    const gl = document.createElement('canvas').getContext('webgl2') as WebGL2RenderingContext
    const info = gl?.getExtension('WEBGL_debug_renderer_info')
    return info ? String(gl.getParameter(info.UNMASKED_RENDERER_WEBGL)) : 'unknown'
  })
  console.log(`ATTRGPU | ${renderer}`)
  await page.waitForFunction(
    (n) =>
      Number(
        document.querySelector('[data-testid="node-count"]')?.textContent?.match(/\d+/)?.[0] ?? 0,
      ) >= n,
    total,
    { timeout: 60_000 },
  )
  await page.waitForTimeout(3000)

  // Arm the counters only now: the initial load and settle are not what we study.
  await page.evaluate(() => {
    ;(window as unknown as { __perf: Record<string, unknown> }).__perf = {}
  })

  // A handful of pushes, exactly as the [15] latency case does.
  // NEW = node count changes each time; UPDATE = count unchanged. If render() is
  // only expensive when the count changes, the cost is re-initialisation on
  // resize — which is exactly the "full reset" [7-D] tells us to avoid.
  const mode = process.env.ATTR_MODE ?? 'new'
  for (let i = 0; i < 10; i += 1) {
    if (mode === 'update') {
      await callTool('update_node', { id: 'ATTR.5', patch: { set: { label: `changed${i}` } } })
    } else {
      await callTool('push_node', { id: `ATTRP.${Date.now()}.${i}`, label: `p${i}`, type: 'probe' })
    }
    await page.waitForTimeout(300)
  }
  console.log(`ATTRMODE | ${mode}`)

  const perf = await page.evaluate(
    () => (window as unknown as { __perf: Record<string, { n: number; ms: number }> }).__perf,
  )

  const rows = Object.entries(perf)
    .map(([bucket, { n, ms }]) => ({ bucket, n, total: ms, avg: n ? ms / n : 0 }))
    .sort((a, b) => b.total - a.total)

  console.log('\n=== [TASK N] flush 경로 시간 귀속 (1K 노드, push 10회, 3초 관측) ===')
  for (const r of rows) {
    console.log(
      `ATTR | ${r.bucket.padEnd(34)} calls=${String(r.n).padStart(5)}  total=${r.total.toFixed(1).padStart(8)}ms  avg=${r.avg.toFixed(2).padStart(7)}ms`,
    )
  }
})
