import { expect, test } from '@playwright/test'
import { BASE, callTool, connectMcp } from './mcpClient'

test('진단: 100K 구조변경 flush 시간 귀속 (effect 버킷)', async ({ page }) => {
  test.setTimeout(600_000)
  await connectMcp()
  await callTool('clear_all', {})
  await callTool('import_from_file', { path: 'perf100k.json' })
  await page.goto(BASE)
  await expect(page.getByTestId('ws-status')).toHaveAttribute('data-connected', 'true')
  await expect(page.getByTestId('node-count')).toHaveText('nodes 100000', { timeout: 300_000 })
  await page.waitForTimeout(8000)
  await page.evaluate(() => { (window as unknown as { __perf: Record<string, unknown> }).__perf = {} })
  for (let i = 0; i < 10; i++) {
    await callTool('push_node', { id: `BKT.${Date.now()}.${i}`, label: `p${i}`, type: 'probe' })
    await page.waitForTimeout(400)
  }
  const perf = await page.evaluate(() => (window as unknown as { __perf: Record<string, { n: number; ms: number }> }).__perf)
  const rows = Object.entries(perf).map(([b, v]) => ({ b, n: v.n, total: v.ms, avg: v.n ? v.ms / v.n : 0 })).sort((a, b) => b.avg - a.avg)
  console.log('\n=== [M3a] 100K 구조변경 flush 버킷 (push 10회, per-push avg) ===')
  for (const r of rows) console.log(`BKT100K | ${r.b.padEnd(32)} n=${r.n} avg=${r.avg.toFixed(1)}ms`)
  const total = perf['effect(total)']?.ms / perf['effect(total)']?.n
  const timedSum = ['effect:buildGraphArrays','effect:applyHighlight','effect:render','effect:trackPointPositions','effect:keepFramed','effect:refreshLabels'].reduce((s,k)=> s + (perf[k] ? perf[k].ms/perf[k].n : 0), 0)
  console.log(`BKT100K | GPU 업로드 갭 (setPointPositions/setLinks, 미계측) = effect(total) ${total.toFixed(1)}ms - timed합 ${timedSum.toFixed(1)}ms = ${(total-timedSum).toFixed(1)}ms`)
  expect(perf['effect(total)']).toBeTruthy()
})
