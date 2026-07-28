/**
 * ★ [15] M3 KPI 실측 — 100K 규모 (TASK M3a). 측정 전용, 튜닝 아님.
 *
 * Extends the [15] measurement methodology (TASK M/N, perf.spec.ts) to the M3 scale:
 * 100K nodes on a real cosmos.gl/WebGL context (RTX GPU via --use-angle=d3d11), a
 * live `visualizebetter serve`, real MCP + WebSocket, the browser's own rAF clock. Every
 * number is a measurement of the real path; a miss is reported as a miss.
 *
 * The 100K graph is loaded through the [5-E] import_from_file path (the M3 KPI's own
 * ingest route), then rendered from the [8-C] /graph.json resync — exactly what a
 * user's machine does. Backend legs (import time / filter / snapshot / RSS) are
 * measured separately (scratchpad/backend_metrics.py + serve RSS); this file owns the
 * render / latency / memory / [7-D] settle legs that need the real GPU + browser.
 */

import { expect, test, type Page } from '@playwright/test'
import { BASE, callTool, connectMcp } from './mcpClient'

const NODE_TARGET = 100_000
const DROPPED_FRAME_MS = 33 // <30fps
const IMPORT_FILE = 'perf100k.json' // pre-placed in serve's data dir

interface FrameStats {
  frames: number; dropped: number; droppedPct: number
  medianMs: number; p95Ms: number; maxMs: number; medianFps: number
}

const results: Record<string, string> = {}
function record(metric: string, value: string) {
  results[metric] = value
  console.log(`[M3a] ${metric}: ${value}`)
}

function frameStats(deltas: number[]): FrameStats {
  const samples = deltas.slice(1) // first delta spans probe install
  const sorted = [...samples].sort((a, b) => a - b)
  const at = (q: number) => sorted[Math.min(sorted.length - 1, Math.floor(sorted.length * q))]
  const dropped = samples.filter((d) => d > DROPPED_FRAME_MS).length
  return {
    frames: samples.length, dropped, droppedPct: (dropped / samples.length) * 100,
    medianMs: at(0.5), p95Ms: at(0.95), maxMs: Math.max(...samples), medianFps: 1000 / at(0.5),
  }
}
function fmt(s: FrameStats): string {
  return `${s.medianFps.toFixed(1)} FPS (median ${s.medianMs.toFixed(1)}ms, p95 ${s.p95Ms.toFixed(1)}ms, ` +
    `max ${s.maxMs.toFixed(1)}ms), dropped ${s.dropped}/${s.frames} (${s.droppedPct.toFixed(1)}%)`
}
function median(v: number[]): number { return [...v].sort((a, b) => a - b)[Math.floor(v.length / 2)] }

let page: Page

async function startFrameProbe() {
  await page.evaluate(() => {
    const w = window as unknown as { __frames: number[]; __probing: boolean }
    w.__frames = []; w.__probing = true
    let last = performance.now()
    const tick = (now: number) => { w.__frames.push(now - last); last = now; if (w.__probing) requestAnimationFrame(tick) }
    requestAnimationFrame(tick)
  })
}
async function stopFrameProbe(): Promise<number[]> {
  return page.evaluate(() => {
    const w = window as unknown as { __frames: number[]; __probing: boolean }
    w.__probing = false; return w.__frames
  })
}
/** Continuous pan+zoom so cosmos redraws every frame — a real render measurement. */
async function panZoom(durationMs: number) {
  const box = await page.getByTestId('cosmos-container').boundingBox()
  if (!box) throw new Error('overview canvas has no box')
  const cx = box.x + box.width / 2, cy = box.y + box.height / 2
  const deadline = Date.now() + durationMs
  await page.mouse.move(cx, cy)
  let i = 0
  while (Date.now() < deadline) {
    await page.mouse.down()
    for (let s = 0; s < 8 && Date.now() < deadline; s += 1) {
      i += 1
      await page.mouse.move(cx + Math.sin(i / 4) * 140, cy + Math.cos(i / 5) * 90)
    }
    await page.mouse.up()
    await page.mouse.wheel(0, i % 2 === 0 ? -120 : 120)
  }
}
/** push → DOM(nodeCount) reflect, single page clock (as perf.spec.ts (3)). */
async function measurePushLatency(count: number, tag: string): Promise<number[]> {
  const samples: number[] = []
  for (let i = 0; i < count; i += 1) {
    await page.evaluate(() => {
      const w = window as unknown as { __armedAt: number; __latency: number | null }
      w.__latency = null
      const t = document.querySelector('[data-testid="node-count"]')
      if (!t) throw new Error('node-count not found')
      w.__armedAt = performance.now()
      new MutationObserver(() => { if (w.__latency === null) w.__latency = performance.now() - w.__armedAt })
        .observe(t, { childList: true, subtree: true, characterData: true })
    })
    await callTool('push_node', { id: `${tag}.${Date.now()}.${i}`, label: `lat${i}`, type: 'probe' })
    await page.waitForFunction(
      () => (window as unknown as { __latency: number | null }).__latency !== null, undefined, { timeout: 60_000 })
    samples.push(await page.evaluate(() => (window as unknown as { __latency: number }).__latency))
  }
  return samples
}

test.describe.configure({ mode: 'serial' })

test.beforeAll(async ({ browser }) => {
  test.setTimeout(600_000)
  await connectMcp()
  await callTool('clear_all', {})

  // --- (2) [15] 배치 import 100K < 30s — the real [5-E] import_from_file path ---
  const importStart = Date.now()
  const imp = await callTool('import_from_file', { path: IMPORT_FILE })
  const importMs = Date.now() - importStart
  record('(2) import_from_file 100K (server in-process)', `${(importMs / 1000).toFixed(2)}s → ${JSON.stringify(imp)}`)
  record('  → (2) 판정 (KPI <30s)', importMs < 30_000 ? `PASS (${(importMs / 1000).toFixed(2)}s)` : `FAIL (${(importMs / 1000).toFixed(2)}s)`)

  page = await browser.newPage()
  await page.goto(BASE)

  // ★ GPU validity gate — a SwiftShader (CPU) figure would measure the harness, not [15].
  const renderer = await page.evaluate(() => {
    const gl = document.createElement('canvas').getContext('webgl2') as WebGL2RenderingContext
    const info = gl?.getExtension('WEBGL_debug_renderer_info')
    return info ? String(gl.getParameter(info.UNMASKED_RENDERER_WEBGL)) : 'unknown'
  })
  record('(0) WebGL 렌더러 (측정 유효성 전제)', renderer)
  expect(renderer, 'SwiftShader = CPU — [15] 는 GPU 전제라 무효').not.toContain('SwiftShader')

  await expect(page.getByTestId('ws-status')).toHaveAttribute('data-connected', 'true')
  // (2b) server → browser displayed: /graph.json resync fetch + parse + store + first render.
  const loadStart = Date.now()
  await expect(page.getByTestId('node-count')).toHaveText(`nodes ${NODE_TARGET}`, { timeout: 300_000 })
  record('(2b) 서버→브라우저 표시(100K resync fetch+parse+apply)', `${((Date.now() - loadStart) / 1000).toFixed(2)}s`)
  // Let the initial force layout settle so (1) measures the steady-state render.
  await page.waitForTimeout(8000)
})

test.afterAll(async () => {
  console.log('\n=== [15] M3 KPI 100K 실측 (render/latency/memory/settle) ===')
  for (const [m, v] of Object.entries(results)) console.log(`  ${m}\n    → ${v}`)
  await page?.close()
})

// --- (1) ★ [15] M3 핵심 KPI: 100K 노드 렌더 >=30 FPS ---
test('★ (1) 100K 정적 렌더 프레임율 (pan/zoom) — 목표 >=30 FPS', async () => {
  test.setTimeout(600_000)
  await startFrameProbe()
  await panZoom(6000)
  const stats = frameStats(await stopFrameProbe())
  record('★ (1) 100K 정적 렌더 (pan/zoom, push 없음)', fmt(stats))
  record('  → (1) 판정 (KPI >=30 FPS)', stats.medianFps >= 30 ? `PASS (${stats.medianFps.toFixed(1)} >= 30)` : `FAIL (${stats.medianFps.toFixed(1)} < 30)`)
  expect(stats.frames).toBeGreaterThan(30) // probe ran; not a target assertion
})

// --- (5) [15] push → 화면 반영 @100K ---
test('(5) push → 화면 반영 지연 @100K — 목표 <100ms', async () => {
  test.setTimeout(600_000)
  const samples = await measurePushLatency(20, 'LAT100K')
  const sorted = [...samples].sort((a, b) => a - b)
  const mid = sorted[Math.floor(sorted.length / 2)]
  record('(5) push → 화면 반영 @100K (20 samples)', `median ${mid.toFixed(1)}ms, p95 ${sorted[Math.floor(sorted.length * 0.95)].toFixed(1)}ms, max ${sorted[sorted.length - 1].toFixed(1)}ms`)
  record('  → (5) 판정 (<100ms)', mid < 100 ? `PASS (${mid.toFixed(1)}ms)` : `FAIL (${mid.toFixed(1)}ms)`)
  expect(samples).toHaveLength(20)
})

// --- (6f) 프론트엔드 메모리 @100K ---
test('(6f) 프론트엔드 JS heap @100K', async () => {
  const heap = await page.evaluate(() => (performance as unknown as { memory?: { usedJSHeapSize: number; totalJSHeapSize: number } }).memory)
  record('(6f) 프론트 usedJSHeapSize @100K', heap ? `${(heap.usedJSHeapSize / 1e6).toFixed(0)} MB (total ${(heap.totalJSHeapSize / 1e6).toFixed(0)} MB)` : 'performance.memory 미제공')
  expect(true).toBe(true)
})

// --- (7) [7-D] 상태기계 settle @100K: 버스트 → SETTLING → FROZEN 비용 ---
test('(7) [7-D] settle @100K — 재개 스톨 + 100K 수렴 + 위치 readback', async () => {
  test.setTimeout(600_000)
  // Re-arm the harness counters after the initial load's settle.
  await page.evaluate(() => { (window as unknown as { __perf: Record<string, unknown> }).__perf = {} })
  // A small burst returns the layout to INGESTING; after the debounce it enters
  // SETTLING and must arrange the whole 100K before FROZEN. That whole cost is the
  // [7-D] settle at scale — measured, not hidden.
  const burstDone = Date.now()
  await callTool('push_batch', { nodes: Array.from({ length: 50 }, (_, i) => ({ id: `BURST.${Date.now()}.${i}`, label: `b${i}`, type: 'class' })) })
  await page.waitForFunction(
    () => (window as unknown as { __perf: Record<string, unknown> }).__perf['settle:total(→readable)'] !== undefined,
    undefined, { timeout: 400_000 })
  const readableAfterMs = Date.now() - burstDone
  const perf = await page.evaluate(() => (window as unknown as { __perf: Record<string, { n: number; ms: number }> }).__perf)
  const avg = (k: string) => (perf[k] ? perf[k].ms / perf[k].n : Number.NaN)
  record('(7) settle 재개 스톨 setConfig(sim on) @100K', `${avg('settle:enable(sim on)').toFixed(1)}ms`)
  record('(7) settle 레이아웃 수렴(→readable) @100K', `${avg('settle:total(→readable)').toFixed(0)}ms`)
  record('(7) settle 위치 회수 readback @100K', `${avg('capturePositions').toFixed(1)}ms`)
  record('(7) 버스트 종료→읽을 수 있기까지 @100K', `${readableAfterMs}ms (debounce 포함)`)
  expect(perf['settle:total(→readable)']).toBeTruthy()
})
