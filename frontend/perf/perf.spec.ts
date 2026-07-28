/**
 * ★ [15] M1 정량 지표 실측 — the measurement [7-D] says is the verification.
 *
 * [7-D]: "인수 기준 (Playwright + 성능 계측, M1): 10K 노드 그래프에 초당 1000건
 * 라이브 push 를 흘리는 동안 overview 상호작용(pan/zoom) 프레임 드랍 없이 유지.
 * 이 테스트 통과가 D2/배칭 전략의 검증." That test had never been run — D2 (React 19)
 * and the batching strategy were locked decisions resting on an unmeasured claim.
 *
 * Everything here is real: a live `visualizebetter serve`, real MCP calls, a real
 * WebSocket, cosmos.gl on a real WebGL context, and the browser's own rAF clock.
 *
 * These tests measure; they do not gate. A number that misses its target is
 * reported as a miss — that is the deliverable, not a failure to hide. Only a
 * broken *measurement* fails the run.
 */

import { expect, test } from '@playwright/test'
import { BASE, callTool, connectMcp, nodeSpecs } from './mcpClient'

const NODE_TARGET = 10_000
const BATCH_SIZE = 1000 // [5-A] MAX_BATCH_ITEMS
const PUSH_RATE = 1000 // [15] 초당 1000건
const LOAD_SECONDS = 5
const DROPPED_FRAME_MS = 33 // <30fps

const PREFIX = 'PERF'

interface FrameStats {
  frames: number
  dropped: number
  droppedPct: number
  medianMs: number
  p95Ms: number
  maxMs: number
  medianFps: number
}

const results: Record<string, string> = {}

function record(metric: string, value: string) {
  results[metric] = value
  console.log(`[15] ${metric}: ${value}`)
}

function frameStats(deltas: number[]): FrameStats {
  // The first delta spans probe installation, not a rendered frame.
  const samples = deltas.slice(1)
  const sorted = [...samples].sort((a, b) => a - b)
  const at = (q: number) => sorted[Math.min(sorted.length - 1, Math.floor(sorted.length * q))]
  const dropped = samples.filter((d) => d > DROPPED_FRAME_MS).length
  return {
    frames: samples.length,
    dropped,
    droppedPct: (dropped / samples.length) * 100,
    medianMs: at(0.5),
    p95Ms: at(0.95),
    maxMs: Math.max(...samples),
    medianFps: 1000 / at(0.5),
  }
}

function fmt(stats: FrameStats): string {
  return (
    `${stats.medianFps.toFixed(1)} FPS (median frame ${stats.medianMs.toFixed(1)}ms, ` +
    `p95 ${stats.p95Ms.toFixed(1)}ms, max ${stats.maxMs.toFixed(1)}ms), ` +
    `dropped ${stats.dropped}/${stats.frames} (${stats.droppedPct.toFixed(1)}%)`
  )
}

let page: import('@playwright/test').Page

/** Collect rAF deltas in the page while the test drives it from outside. */
async function startFrameProbe() {
  await page.evaluate(() => {
    const w = window as unknown as { __frames: number[]; __probing: boolean }
    w.__frames = []
    w.__probing = true
    let last = performance.now()
    const tick = (now: number) => {
      w.__frames.push(now - last)
      last = now
      if (w.__probing) requestAnimationFrame(tick)
    }
    requestAnimationFrame(tick)
  })
}

async function stopFrameProbe(): Promise<number[]> {
  return page.evaluate(() => {
    const w = window as unknown as { __frames: number[]; __probing: boolean }
    w.__probing = false
    return w.__frames
  })
}

/**
 * Continuous pan + zoom over the overview for `durationMs`.
 *
 * Pan and zoom force cosmos to redraw every frame, which is what makes the frame
 * deltas a measurement of rendering rather than of an idle rAF loop — a settled
 * simulation stops drawing, and an idle browser reports a flattering 60fps that
 * says nothing about 10K nodes.
 */
async function panZoom(durationMs: number) {
  const box = await page.getByTestId('cosmos-container').boundingBox()
  if (!box) throw new Error('overview canvas has no box')
  const cx = box.x + box.width / 2
  const cy = box.y + box.height / 2

  const deadline = Date.now() + durationMs
  await page.mouse.move(cx, cy)
  let i = 0
  while (Date.now() < deadline) {
    await page.mouse.down()
    for (let step = 0; step < 8 && Date.now() < deadline; step += 1) {
      i += 1
      await page.mouse.move(cx + Math.sin(i / 4) * 140, cy + Math.cos(i / 5) * 90)
    }
    await page.mouse.up()
    await page.mouse.wheel(0, i % 2 === 0 ? -120 : 120)
  }
}

/**
 * Push at a target rate and report what was actually achieved.
 *
 * The achieved rate is reported, never assumed: if the harness cannot produce
 * 1000 push/s then the frame numbers describe a lighter load than [15] asks for,
 * and saying so is the difference between a measurement and a decoration.
 */
async function pushAtRate(durationMs: number, rate: number, prefix: string) {
  const total = Math.round((durationMs / 1000) * rate)
  const started = performance.now()
  let sent = 0
  let failed = 0
  let cursor = 0

  async function worker() {
    for (;;) {
      const index = cursor
      cursor += 1
      if (index >= total) return
      const due = started + (index / rate) * 1000
      const wait = due - performance.now()
      if (wait > 0) await new Promise((r) => setTimeout(r, wait))
      try {
        await callTool('push_node', {
          id: `${prefix}.${index}`,
          label: `live${index}`,
          type: 'live',
        })
        sent += 1
      } catch {
        failed += 1
      }
    }
  }

  // Concurrent: a sequential loop is bounded by round-trip latency, which would
  // cap the load far below 1000/s and silently make the test easier.
  await Promise.all(Array.from({ length: 32 }, worker))
  const elapsedMs = performance.now() - started
  return { sent, failed, achievedRate: sent / (elapsedMs / 1000), elapsedMs }
}

test.describe.configure({ mode: 'serial' })

test.beforeAll(async ({ browser }) => {
  await connectMcp()
  await callTool('clear_all', {})

  // --- (5) 배치 유입 10K (참고) ---
  // [15]'s stated criterion is "배치 import 10K < 5s (CLI / import_from_file 경로)",
  // and import_from_file does not exist yet — so this measures push_batch instead
  // and is reported as a reference number, not as that criterion.
  const ingestStart = performance.now()
  for (let from = 0; from < NODE_TARGET; from += BATCH_SIZE) {
    await callTool('push_batch', { nodes: nodeSpecs(PREFIX, from, BATCH_SIZE) })
  }
  const ingestMs = performance.now() - ingestStart
  record(
    '(5) 배치 유입 10K (참고, push_batch 1000×10)',
    `${(ingestMs / 1000).toFixed(2)}s — ${Math.round(NODE_TARGET / (ingestMs / 1000))} nodes/s`,
  )

  page = await browser.newPage()
  await page.goto(BASE)

  // ★ Establish that this is a GPU measurement before measuring anything.
  // Headless Chromium falls back to SwiftShader (a CPU rasterizer) by default,
  // and every frame number here would then describe software rendering while
  // [15]/D2 rest on "FPS 는 cosmos.gl(WebGL/GPU)이 결정한다". A silent fallback
  // would not fail — it would quietly report the wrong thing, which is worse.
  const renderer = await page.evaluate(() => {
    const gl = document.createElement('canvas').getContext('webgl2') as WebGL2RenderingContext
    const info = gl?.getExtension('WEBGL_debug_renderer_info')
    return info ? String(gl.getParameter(info.UNMASKED_RENDERER_WEBGL)) : 'unknown'
  })
  record('(0) WebGL 렌더러 (측정 유효성 전제)', renderer)
  expect(renderer, 'SwiftShader = CPU 래스터라이저 — [15] 는 GPU 전제라 측정이 무효다').not.toContain(
    'SwiftShader',
  )

  await expect(page.getByTestId('ws-status')).toHaveAttribute('data-connected', 'true')
  // The browser loads the 10K via the [8-C] resync snapshot, not by replaying pushes.
  await expect(page.getByTestId('node-count')).toHaveText(`nodes ${NODE_TARGET}`, {
    timeout: 60_000,
  })
  // Let the force layout settle so the render cost is the steady state.
  await page.waitForTimeout(3000)
})

test.afterAll(async () => {
  console.log('\n=== [15] M1 정량 지표 실측 ===')
  for (const [metric, value] of Object.entries(results)) console.log(`  ${metric}\n    → ${value}`)
  await page?.close()
})

// --- (2) [15] 10K 노드 렌더 30 FPS 이상 ---

test('(2) 10K 노드 정적 렌더 프레임율 — 목표 >=30 FPS', async () => {
  await startFrameProbe()
  await panZoom(5000)
  const stats = frameStats(await stopFrameProbe())

  record('(2) 10K 정적 렌더 (pan/zoom, push 없음) — 목표 >=30 FPS', fmt(stats))
  record(
    '  → (2) 판정',
    stats.medianFps >= 30 ? `통과 (${stats.medianFps.toFixed(1)} >= 30)` : `미달 (${stats.medianFps.toFixed(1)} < 30)`,
  )
  expect(stats.frames).toBeGreaterThan(30) // the probe ran; not a target assertion
})

// --- (3) [15] Node push → 화면 반영 < 100ms ---

/** push → DOM(nodeCount) 반영까지, 페이지 클럭 단일 기준. */
async function measurePushLatency(count: number, tag: string): Promise<number[]> {
  const samples: number[] = []

  for (let i = 0; i < count; i += 1) {
    // Armed in the page, so the elapsed time is measured on one clock. It starts
    // fractionally before the push is issued (one CDP hop), which overstates the
    // latency slightly — conservative, so a pass is a real pass.
    await page.evaluate(() => {
      const w = window as unknown as { __armedAt: number; __latency: number | null }
      w.__latency = null
      const target = document.querySelector('[data-testid="node-count"]')
      if (!target) throw new Error('node-count not found')
      w.__armedAt = performance.now()
      const observer = new MutationObserver(() => {
        if (w.__latency === null) w.__latency = performance.now() - w.__armedAt
      })
      // nodeCount only changes on the [7-D] rAF flush, so this fires when the
      // pushed node has actually reached the store and re-rendered React.
      observer.observe(target, { childList: true, subtree: true, characterData: true })
    })

    await callTool('push_node', { id: `${tag}.${Date.now()}.${i}`, label: `lat${i}`, type: 'probe' })

    await page.waitForFunction(
      () => (window as unknown as { __latency: number | null }).__latency !== null,
      undefined,
      { timeout: 30_000 },
    )
    samples.push(
      await page.evaluate(() => (window as unknown as { __latency: number }).__latency),
    )
  }
  return samples
}

function median(values: number[]): number {
  return [...values].sort((a, b) => a - b)[Math.floor(values.length / 2)]
}

test('(3) node push → 화면 반영 지연 — 목표 <100ms', async () => {
  const samples = await measurePushLatency(20, 'LAT')
  const sorted = [...samples].sort((a, b) => a - b)
  const mid = sorted[Math.floor(sorted.length / 2)]

  record(
    '(3) push → 화면 반영 @10K (20 samples) — 목표 <100ms',
    `median ${mid.toFixed(1)}ms, p95 ${sorted[Math.floor(sorted.length * 0.95)].toFixed(1)}ms, max ${sorted[sorted.length - 1].toFixed(1)}ms`,
  )
  record('  → (3) 판정', mid < 100 ? `통과 (${mid.toFixed(1)}ms < 100ms)` : `미달 (${mid.toFixed(1)}ms >= 100ms)`)
  expect(samples).toHaveLength(20)
})

// --- (6) [15] MCP tool 응답 (단일 push) < 50ms ---
// A [15] M1 criterion the dispatch did not list, measured because (1a)'s achieved
// rate depends on it: if one push costs >50ms server-side, no client-side fix
// reaches 1000 push/s.

test('(6) MCP tool 응답 — 단일 push, 목표 <50ms', async () => {
  const samples: number[] = []
  for (let i = 0; i < 20; i += 1) {
    const start = performance.now()
    await callTool('push_node', { id: `RTT.${Date.now()}.${i}`, label: `rtt${i}`, type: 'probe' })
    samples.push(performance.now() - start)
  }
  const mid = median(samples)
  record(
    '(6) MCP tool 응답 (단일 push, 순차 20회) — 목표 <50ms',
    `median ${mid.toFixed(1)}ms, max ${Math.max(...samples).toFixed(1)}ms`,
  )
  record('  → (6) 판정', mid < 50 ? `통과 (${mid.toFixed(1)}ms < 50ms)` : `미달 (${mid.toFixed(1)}ms >= 50ms)`)
  expect(samples).toHaveLength(20)
})

// --- (1) ★ [7-D]/[15] 핵심 인수기준 ---

test('★ (1) 10K + 1000 push/s 중 pan/zoom 무렉 — [7-D] D2/배칭 전략 검증', async () => {
  const prefix = `LIVE.${Date.now()}`

  await startFrameProbe()
  const [load] = await Promise.all([
    pushAtRate(LOAD_SECONDS * 1000, PUSH_RATE, prefix),
    panZoom(LOAD_SECONDS * 1000),
  ])
  const stats = frameStats(await stopFrameProbe())

  record(
    '(1a) 실제 달성 push rate',
    `${load.achievedRate.toFixed(0)}/s (목표 ${PUSH_RATE}/s, sent ${load.sent}, failed ${load.failed}, ${(load.elapsedMs / 1000).toFixed(2)}s)`,
  )
  record('★ (1) 10K + 1000 push/s 중 pan/zoom — 목표 프레임 드랍 없음', fmt(stats))
  record(
    '  → (1) 판정',
    stats.dropped === 0
      ? `통과 (드랍 0)`
      : `미달 (드랍 ${stats.dropped}/${stats.frames} = ${stats.droppedPct.toFixed(1)}%, >33ms)`,
  )

  // The measurement must be valid even when the result is bad: too few frames,
  // or a load far below target, would make the verdict meaningless.
  expect(stats.frames).toBeGreaterThan(30)
  expect(load.sent).toBeGreaterThan(0)
})

// --- (4) [15] 스냅샷 저장/복원 10K < 2s ---

test('(4) 스냅샷 save/restore 10K 왕복 — 목표 <2s', async () => {
  const saveStart = performance.now()
  const saved = await callTool('save_snapshot', { name: `perf-${Date.now()}` })
  const saveMs = performance.now() - saveStart
  const snapshotId = saved.snapshot_id as string

  const loadStart = performance.now()
  await callTool('load_snapshot', { snapshot_id: snapshotId })
  const loadMs = performance.now() - loadStart

  const totalMs = saveMs + loadMs
  record(
    '(4) 스냅샷 save/restore 왕복 — 목표 <2s',
    `save ${saveMs.toFixed(0)}ms + restore ${loadMs.toFixed(0)}ms = ${(totalMs / 1000).toFixed(2)}s`,
  )
  record(
    '  → (4) 판정',
    totalMs < 2000 ? `통과 (${(totalMs / 1000).toFixed(2)}s < 2s)` : `미달 (${(totalMs / 1000).toFixed(2)}s >= 2s)`,
  )
  // restore is not just a load: [23-C] takes a recovery snapshot of the live 10K
  // graph first, so what a user pays for load_snapshot includes that save.
  record('  (4) 주: restore 는 [23-C] 파괴적 작업 직전 auto-snapshot(10K save) 포함', 'measured as user-visible cost')
  expect(snapshotId).toBeTruthy()
})

// --- 진단: 미달 원인이 그래프 크기에 비례하는가 ---

test('진단: push 반영 비용이 노드 수에 비례하는가 (1K vs 10K)', async () => {
  // If one push costs the same at 1K as at 10K, the bottleneck is fixed overhead
  // (WS, flush scheduling). If it scales with N, the flush is doing O(N) work per
  // push — which is what [7-D]'s "Cosmos: incremental update (node add/remove
  // without full reset)" exists to forbid. That distinction decides the fix.
  await callTool('clear_all', {})
  await expect(page.getByTestId('node-count')).toHaveText('nodes 0', { timeout: 60_000 })

  await callTool('push_batch', { nodes: nodeSpecs('SMALL', 0, 1000) })
  await expect(page.getByTestId('node-count')).toHaveText('nodes 1000', { timeout: 60_000 })
  await page.waitForTimeout(2000)

  const small = median(await measurePushLatency(10, 'LATS'))
  record('(진단) push → 화면 반영 @1K', `median ${small.toFixed(1)}ms`)
  record(
    '(진단) 비교',
    `@1K ${small.toFixed(1)}ms vs @10K (위 (3) 참조) — 비례하면 flush 당 O(N) 재구축이 원인`,
  )
  expect(small).toBeGreaterThan(0)
})

test('진단: 지연 200ms 의 서버 구간 vs 클라이언트 구간', async () => {
  // A flat ~200ms is a fixed interval somewhere, not work. This splits it at the
  // wire: an independent probe socket timestamps when the server actually emits
  // the event, so the server leg (MCP → hub flush → WS) and the client leg (WS →
  // store flush → React) can be told apart instead of guessed at.
  const wsUrl = `${BASE.replace(/^http/, 'ws')}/live`
  await page.evaluate((url) => {
    const w = window as unknown as { __probeWs: WebSocket; __wsAt: number | null }
    const socket = new WebSocket(url)
    w.__probeWs = socket
    w.__wsAt = null
    socket.onmessage = () => {
      const win = window as unknown as { __wsAt: number | null }
      if (win.__wsAt === null) win.__wsAt = performance.now()
    }
    return new Promise<void>((resolve) => {
      socket.onopen = () => resolve()
    })
  }, wsUrl)

  async function splitAt(label: string, samples: number) {
    const server: number[] = []
    const client: number[] = []
    for (let i = 0; i < samples; i += 1) {
      const [s, c] = await onePush(`${label}${i}`)
      server.push(s)
      client.push(c)
    }
    record(
      `(진단) 지연 분해 @${label}`,
      `서버(push→WS) ${median(server).toFixed(1)}ms + 클라이언트(WS→DOM) ${median(client).toFixed(1)}ms`,
    )
    return { server: median(server), client: median(client) }
  }

  async function onePush(tag: string): Promise<[number, number]> {
    await page.evaluate(() => {
      const w = window as unknown as {
        __armedAt: number
        __latency: number | null
        __wsAt: number | null
      }
      w.__latency = null
      w.__wsAt = null
      const target = document.querySelector('[data-testid="node-count"]')!
      w.__armedAt = performance.now()
      new MutationObserver(() => {
        if (w.__latency === null) w.__latency = performance.now() - w.__armedAt
      }).observe(target, { childList: true, subtree: true, characterData: true })
    })

    await callTool('push_node', { id: `SPLIT.${Date.now()}.${tag}`, label: `s${tag}`, type: 'probe' })

    await page.waitForFunction(
      () => {
        const w = window as unknown as { __latency: number | null; __wsAt: number | null }
        return w.__latency !== null && w.__wsAt !== null
      },
      undefined,
      { timeout: 30_000 },
    )
    const [wsMs, domMs] = await page.evaluate(() => {
      const w = window as unknown as { __armedAt: number; __latency: number; __wsAt: number }
      return [w.__wsAt - w.__armedAt, w.__latency]
    })
    return [wsMs, domMs - wsMs]
  }

  // Tiny graph: whatever remains here is fixed per-flush overhead, not the cost
  // of the data. If the client leg is already ~150ms at 10 nodes, no amount of
  // incremental-update work on large graphs would fix it.
  await callTool('clear_all', {})
  await expect(page.getByTestId('node-count')).toHaveText('nodes 0', { timeout: 60_000 })
  await callTool('push_batch', { nodes: nodeSpecs('TINY', 0, 10) })
  await expect(page.getByTestId('node-count')).toHaveText('nodes 10', { timeout: 60_000 })
  await page.waitForTimeout(1500)
  const tiny = await splitAt('10노드', 10)

  await callTool('push_batch', { nodes: nodeSpecs('BIG', 0, 1000) })
  // The probe pushes above added nodes of their own, so wait on the magnitude
  // rather than an exact count.
  await page.waitForFunction(
    () => Number(document.querySelector('[data-testid="node-count"]')?.textContent?.match(/\d+/)?.[0] ?? 0) >= 1000,
    undefined,
    { timeout: 60_000 },
  )
  await page.waitForTimeout(1500)
  const big = await splitAt('1K노드', 10)

  record(
    '(진단) 결론',
    `클라이언트 구간 @10노드 ${tiny.client.toFixed(1)}ms vs @1K ${big.client.toFixed(1)}ms — ` +
      `근접하면 flush 당 고정 비용(GPU 동기화/sim 재시작), 비례하면 O(N) 재구축`,
  )
  expect(tiny.client).toBeGreaterThan(0)
})
