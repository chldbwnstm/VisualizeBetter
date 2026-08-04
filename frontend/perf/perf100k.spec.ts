/**
 * ★ [15] M3 KPI 실측 — 100K 규모 (TASK M3a). 측정 전용, 튜닝 아님.
 *
 * Extends the [15] measurement methodology (TASK M/N, perf.spec.ts) to the M3 scale:
 * 100K nodes on a real cosmos.gl/WebGL context (RTX GPU via --use-angle=d3d11), a
 * live `visualizebetter serve`, real MCP + WebSocket, the browser's own rAF clock. Every
 * number is a measurement of the real path.
 *
 * ★ [13-B] CH2(5): a miss is no longer only *reported* — the [15] verdicts are
 * `expect.soft` assertions now, so a regression turns this spec red instead of
 * printing FAIL into a console nobody gates on. See `./kpi.ts` for why soft.
 *
 * The 100K graph is loaded through the [5-E] import_from_file path (the M3 KPI's own
 * ingest route), then rendered from the [8-C] /graph.json resync — exactly what a
 * user's machine does. Backend legs (import time / filter / snapshot / RSS) are
 * measured separately (scratchpad/backend_metrics.py + serve RSS); this file owns the
 * render / latency / memory / [7-D] settle legs that need the real GPU + browser.
 */

import { expect, test, type Page } from '@playwright/test'
import { kpiAtLeast, kpiUnder } from './kpi'
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

/**
 * ★ [15] 판정은 측정이 **전부 끝난 뒤** 마지막 테스트가 한꺼번에 한다.
 *
 * 이 파일은 serial 이다(공유 page + 한 번만 적재하는 100K). serial 에서는 한
 * 테스트가 실패하면 **뒤 테스트가 아예 실행되지 않는다** — 실측으로 확인했다:
 * (5) 의 판정을 그 자리에서 단언했더니 (5) 미달과 함께 (6f) 메모리와 (7) settle
 * 이 "did not run" 이 됐다. 회귀를 진단할 때 가장 필요한 게 그 전경인데 KPI 하나가
 * 그것을 지운다. 그래서 측정은 전부 돌리고 판정만 끝에 모은다.
 *
 * (perf.spec.ts 는 serial 이 아니라 판정을 측정 자리에 둔다 — 거기서는 한 테스트의
 * 실패가 다음 테스트를 막지 않으므로 실패가 제 지표 이름을 달고 뜨는 쪽이 낫다.)
 */
const verdicts: { criterion: string; check: () => void }[] = []
function verdict(criterion: string, check: () => void) {
  verdicts.push({ criterion, check })
}

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
/**
 * push → **실제 그려질 때까지**, 페이지 단일 클럭 (계획서 [15 개정] 확정 2).
 *
 * ★ 종료점이 DOM 관측이 아니라 코드다. 예전에는 노드 카운트 텍스트의
 * MutationObserver 로 끝을 잡았는데, 그 자리는 React 가 cosmos effect 를 DOM
 * 커밋과 같은 태스크에 붙여 줄 때만 "그려진 뒤" 다 — 스케줄링이 달라지면 같은
 * 코드가 조용히 다른 양을 잰다. 공표값 73.9ms 가 그 사고였고(effect 앞에서 끊긴
 * 측정), 그래서 재현이 불가능했다. 지금은 OverviewCanvas 가 데이터를 캔버스에
 * 올린 시점에 직접 찍는 `window.__vbPainted` 를 읽는다.
 *
 * 카운터로 판정한다(시각 비교가 아니라): arm 시점의 n 을 기억하고 그보다 커질
 * 때까지 기다린다. 그래야 arm 이전의 페인트를 새 것으로 착각하지 않는다.
 */
interface PaintMark { n: number; at: number }
async function measurePushLatency(count: number, tag: string): Promise<number[]> {
  const samples: number[] = []
  for (let i = 0; i < count; i += 1) {
    await page.evaluate(() => {
      const w = window as unknown as { __armedAt: number; __paintBase: number; __vbPainted?: PaintMark }
      if (!w.__vbPainted) throw new Error('__vbPainted 없음 — 종료점 마크가 사라졌다(측정 무효)')
      w.__paintBase = w.__vbPainted.n
      w.__armedAt = performance.now()
    })
    await callTool('push_node', { id: `${tag}.${Date.now()}.${i}`, label: `lat${i}`, type: 'probe' })
    await page.waitForFunction(
      () => {
        const w = window as unknown as { __paintBase: number; __vbPainted: PaintMark }
        return w.__vbPainted.n > w.__paintBase
      },
      undefined, { timeout: 60_000 })
    samples.push(await page.evaluate(() => {
      const w = window as unknown as { __armedAt: number; __vbPainted: PaintMark }
      return w.__vbPainted.at - w.__armedAt
    }))
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
  verdict('배치 import 100K < 30s', () => kpiUnder(importMs, 30_000, '배치 import 100K < 30s'))

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
  // 프로브가 실제로 돌았는지는 여기서 hard 로 본다 — 프레임이 몇 개 없으면 아래
  // 판정은 측정이 아니라 잡음에 대한 판정이 되고, 그건 뒤 측정을 막을 만하다.
  expect(stats.frames).toBeGreaterThan(30)
  verdict('100K 렌더 >= 30 FPS', () => kpiAtLeast(stats.medianFps, 30, '100K 렌더 >= 30 FPS'))
})

// --- (5) [15] push → 화면 반영 @100K ---
test('(5) push → 화면 반영 지연 @100K — 목표 <100ms', async () => {
  test.setTimeout(600_000)
  const samples = await measurePushLatency(20, 'LAT100K')
  const sorted = [...samples].sort((a, b) => a - b)
  const mid = sorted[Math.floor(sorted.length / 2)]
  record('(5) push → 화면 반영 @100K (20 samples)', `median ${mid.toFixed(1)}ms, p95 ${sorted[Math.floor(sorted.length * 0.95)].toFixed(1)}ms, max ${sorted[sorted.length - 1].toFixed(1)}ms`)
  expect(samples).toHaveLength(20)
  verdict('push → 화면 표시 < 100ms (@100K)', () =>
    kpiUnder(mid, 100, 'push → 화면 표시 < 100ms (@100K)'))
})

// --- (6f) 프론트엔드 메모리 @100K ---
test('(6f) 프론트엔드 JS heap @100K', async () => {
  // ★ 숫자를 **페이지 안에서** 꺼낸다. `performance.memory` 를 통째로 돌려주면
  // 값이 프로토타입의 getter 라 직렬화에서 전부 떨어져 나가고, 남는 것은 빈
  // 객체다. 그래서 이전 판은 "NaN MB (total NaN MB)" 를 기록하고 있었고 —
  // `expect(true).toBe(true)` 라 아무도 몰랐다. 측정이 없는 측정이었다.
  const heap = await page.evaluate(() => {
    const m = (performance as unknown as { memory?: { usedJSHeapSize: number; totalJSHeapSize: number } }).memory
    return m ? { used: m.usedJSHeapSize, total: m.totalJSHeapSize } : null
  })
  record('(6f) 프론트 usedJSHeapSize @100K', heap ? `${(heap.used / 1e6).toFixed(0)} MB (total ${(heap.total / 1e6).toFixed(0)} MB)` : 'performance.memory 미제공')
  // [15] 에 프론트 메모리 인수 기준은 없다 — 이건 보고용 실측이라 문턱을 지어내지
  // 않는다. 대신 **측정이 실제로 일어났는지**를 단언한다. 값을 못 얻은 경우는
  // 조용한 통과가 아니라 skip 으로 보이게 한다.
  test.skip(heap === null, 'performance.memory 미제공 — 이 브라우저에서는 측정 자체가 불가')
  expect(heap!.used, '힙 수치를 얻지 못했다 — 기록된 값이 측정이 아니다').toBeGreaterThan(0)
  expect(heap!.used).toBeLessThanOrEqual(heap!.total)
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

// --- ★ [15] 판정 — 위 측정들을 인수 기준과 대조한다 ---
//
// 여기가 이 파일에서 회귀가 빨간불이 되는 자리다. 이전에는 각 판정이
// `record('… 판정', cond ? 'PASS' : 'FAIL')` 로 콘솔에만 남았고, 콘솔은 아무도
// 빨갛게 만들지 않았다.
test('★ [15] 판정 — 실측을 M3 인수 기준과 대조', async () => {
  // 판정할 것이 실제로 모였는가. 측정 테스트가 안 돌았는데 "미달 0건" 으로 초록인
  // 것이 정확히 고치려는 상태다.
  expect(
    verdicts.map((v) => v.criterion),
    '판정이 모이지 않았다 — 위 측정이 돌지 않았는데 통과할 뻔했다',
  ).toEqual([
    '배치 import 100K < 30s',
    '100K 렌더 >= 30 FPS',
    'push → 화면 표시 < 100ms (@100K)',
  ])
  for (const { check } of verdicts) check()
})
