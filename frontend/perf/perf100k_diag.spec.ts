/**
 * M3a 진단 — 100K 미달 지표(push→표시 209ms)의 병목을 실측 귀속한다. 추측 금지.
 *
 * Splits the push→display latency at the wire (an independent probe socket
 * timestamps the server's emit) into a server leg (MCP → hub flush → WS) and a
 * client leg (WS → store flush → React DOM), then contrasts the client leg at 100K
 * vs a tiny graph. Fixed ⇒ per-flush overhead (rAF window / GPU sync); scaling with
 * N ⇒ O(N) rebuild per push, which [7-D]'s incremental-append path exists to forbid.
 * Also reads the frontend JS heap via CDP (headless_shell hides performance.memory).
 */

import { expect, test, type Page } from '@playwright/test'
import { BASE, callTool, connectMcp, nodeSpecs } from './mcpClient'

const NODE_TARGET = 100_000
const results: Record<string, string> = {}
function record(m: string, v: string) { results[m] = v; console.log(`[M3a-diag] ${m}: ${v}`) }
function median(v: number[]): number { return [...v].sort((a, b) => a - b)[Math.floor(v.length / 2)] }

let page: Page

async function openProbeSocket() {
  const wsUrl = `${BASE.replace(/^http/, 'ws')}/live`
  await page.evaluate((url) => {
    const w = window as unknown as { __probeWs: WebSocket; __wsAt: number | null }
    const s = new WebSocket(url)
    w.__probeWs = s; w.__wsAt = null
    s.onmessage = () => { const win = window as unknown as { __wsAt: number | null }; if (win.__wsAt === null) win.__wsAt = performance.now() }
    return new Promise<void>((res) => { s.onopen = () => res() })
  }, wsUrl)
}

/** One push → [serverLegMs (push→WS emit), clientLegMs (WS→DOM)]. */
async function onePushSplit(tag: string): Promise<[number, number]> {
  await page.evaluate(() => {
    const w = window as unknown as { __armedAt: number; __latency: number | null; __wsAt: number | null }
    w.__latency = null; w.__wsAt = null
    const t = document.querySelector('[data-testid="node-count"]')!
    w.__armedAt = performance.now()
    new MutationObserver(() => { if (w.__latency === null) w.__latency = performance.now() - w.__armedAt })
      .observe(t, { childList: true, subtree: true, characterData: true })
  })
  await callTool('push_node', { id: `SPLIT.${Date.now()}.${tag}`, label: `s${tag}`, type: 'probe' })
  await page.waitForFunction(() => {
    const w = window as unknown as { __latency: number | null; __wsAt: number | null }
    return w.__latency !== null && w.__wsAt !== null
  }, undefined, { timeout: 60_000 })
  const [wsMs, domMs] = await page.evaluate(() => {
    const w = window as unknown as { __armedAt: number; __latency: number; __wsAt: number }
    return [w.__wsAt - w.__armedAt, w.__latency]
  })
  return [wsMs, domMs - wsMs]
}

async function splitAt(label: string, samples: number) {
  const server: number[] = [], client: number[] = []
  for (let i = 0; i < samples; i += 1) { const [s, c] = await onePushSplit(`${label}${i}`); server.push(s); client.push(c) }
  const sv = median(server), cl = median(client)
  record(`지연 분해 @${label}`, `서버(push→WS) ${sv.toFixed(1)}ms + 클라(WS→DOM) ${cl.toFixed(1)}ms = ${(sv + cl).toFixed(1)}ms`)
  return { server: sv, client: cl }
}

test.describe.configure({ mode: 'serial' })

test.beforeAll(async ({ browser }) => {
  test.setTimeout(600_000)
  await connectMcp()
  page = await browser.newPage()
  await page.goto(BASE)
  await expect(page.getByTestId('ws-status')).toHaveAttribute('data-connected', 'true')
  await expect(page.getByTestId('node-count')).toHaveText(new RegExp(`nodes (${NODE_TARGET}|${NODE_TARGET + 1}\\d*|1\\d{5})`), { timeout: 300_000 })
  await page.waitForTimeout(8000)
  await openProbeSocket()
})

test.afterAll(async () => {
  console.log('\n=== [M3a] 진단 요약 ===')
  for (const [m, v] of Object.entries(results)) console.log(`  ${m} → ${v}`)
  await page?.close()
})

test('(6f) 프론트 JS heap @100K (CDP)', async () => {
  const client = await page.context().newCDPSession(page)
  await client.send('Performance.enable')
  const m = (await client.send('Performance.getMetrics')) as unknown as { metrics: { name: string; value: number }[] }
  const used = m.metrics.find((x) => x.name === 'JSHeapUsedSize')?.value ?? 0
  const total = m.metrics.find((x) => x.name === 'JSHeapTotalSize')?.value ?? 0
  const nodes = m.metrics.find((x) => x.name === 'Nodes')?.value ?? 0
  record('(6f) 프론트 JS heap @100K (CDP)', `used ${(used / 1e6).toFixed(0)} MB, total ${(total / 1e6).toFixed(0)} MB, DOM nodes ${nodes}`)
  expect(used).toBeGreaterThan(0)
})

test('★ 진단: push→표시 209ms 를 서버 구간 vs 클라 구간으로 분해 @100K', async () => {
  test.setTimeout(600_000)
  const big = await splitAt('100K', 15)
  record('★ 진단 결론 @100K', `서버 ${big.server.toFixed(1)}ms vs 클라 ${big.client.toFixed(1)}ms — 지배 구간 = ${big.server > big.client ? '서버(push→WS)' : '클라(WS→DOM)'}`)
  expect(big.server + big.client).toBeGreaterThan(0)
})

test('★ 진단: 클라 구간이 N 에 비례하는가 (100K vs 100노드)', async () => {
  test.setTimeout(600_000)
  const big = await splitAt('100Kb', 12)
  // Drop to a tiny graph; the same client leg here is fixed per-flush overhead.
  await callTool('clear_all', {})
  await expect(page.getByTestId('node-count')).toHaveText('nodes 0', { timeout: 120_000 })
  await callTool('push_batch', { nodes: nodeSpecs('SMALL', 0, 100) })
  await page.waitForFunction(() => Number(document.querySelector('[data-testid="node-count"]')?.textContent?.match(/\d+/)?.[0] ?? 0) >= 100, undefined, { timeout: 120_000 })
  await page.waitForTimeout(2000)
  const small = await splitAt('100노드', 12)
  const ratio = big.client / Math.max(0.1, small.client)
  record('★ 진단 O(N) 판정', `클라 @100K ${big.client.toFixed(1)}ms vs @100 ${small.client.toFixed(1)}ms (배율 ${ratio.toFixed(1)}×) — ` +
    `근접(≈1×)이면 flush 당 고정비용, 비례(≫1×)이면 push 당 O(N) 재구축`)
  // [13-B] CH2(5): 이 판정은 **일부러** 단언으로 승격하지 않는다. 위 두 스펙의
  // 판정은 [15] 가 적어 둔 숫자를 옮겨 적는 것이지만, 여기엔 대응하는 인수 기준이
  // 없다 — "배율 몇 배까지 허용" 은 지금 지어내야 하는 값이고, 인수 기준을 새로
  // 만드는 것은 CLAUDE.md 의 STOP&ASK 다. 이건 [7-D] 상태기계를 고르게 만든
  // **진단**이고 그 성격 그대로 둔다.
  // 대신 진단이 성립할 조건은 단언한다: 두 구간이 **둘 다** 실제로 측정됐어야
  // 배율에 의미가 있다. big.client 는 여기서 처음 단언된다 — 100K 구간이 0 이어도
  // 배율은 0× 로 계산되고 테스트는 초록이었다.
  expect(small.client, '@100 구간이 측정되지 않았다 — 배율의 분모가 없다').toBeGreaterThan(0)
  expect(big.client, '@100K 구간이 측정되지 않았다 — 배율의 분자가 없다').toBeGreaterThan(0)
})
