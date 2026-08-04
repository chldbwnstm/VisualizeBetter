/**
 * Completion verification for TASK O — [7-D] 상태기계 정합.
 *
 * Two defects from the TASK N review:
 *  1. Selecting a finding (highlight-only) re-ran the data path, which moved
 *     positions and armed a settle — so a settled graph rearranged itself 500ms
 *     after a click. Selection is an overlay ([23-B] 위상 불변).
 *  2. The positions map was updated in place, so deleted nodes were never
 *     dropped — a slow leak across a long session of churn.
 */

import { act, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest'

/** Records the calls the lifecycle makes, so the test can assert what it did. */
const calls = {
  setPointPositions: 0,
  setPointColors: 0,
  setPointSizes: 0,
  render: 0,
  simulationEnabled: [] as boolean[],
  start: 0,
  /** Last positions handed to the GPU — the only place the call-site's map shows. */
  lastPositions: null as Float32Array | null,
  lastColors: null as Float32Array | null,
  /** [13-B] CH2(3) — indices the component asked to track, for the readback mock. */
  trackedIndices: [] as number[],
  /**
   * [13-B] CH2(3) — what the "GPU" reports back, independent of what was
   * uploaded. That independence is the point of a readback: the simulation moves
   * points, so what comes back is *not* what went in. Leave it null and the mock
   * echoes the last upload; set it and the component sees a moved layout.
   */
  gpuPositions: null as Float32Array | null,
  /** [13-B] CH2(3) — how many times the component read back from the "GPU". */
  readbacks: 0,
  /** [13-B] CH2(3) — label-overlay readbacks, counted separately. */
  labelReadbacks: 0,
  /** [15] (5) 레버 (b) — 카메라 재프레이밍 횟수. push 경로에서 빠졌는지 본다. */
  fitViews: 0,
  /** [15] (5) 레버 (a) — tracking 호출 횟수(무엇을 추적했는지와 별개로). */
  trackCalls: 0,
}

/**
 * cosmos.gl readiness, made controllable ([KI-1]). Real cosmos builds its WebGL
 * device asynchronously and only then flips `isReady`; the mock defaults to ready
 * (so every existing test is unaffected) but a test can start it not-ready and
 * resolve `ready` on cue to exercise the OverviewCanvas readiness gate.
 */
const cosmosReady = { isReady: true, ready: Promise.resolve() as Promise<void> }

vi.mock('@cosmos.gl/graph', () => ({
  Graph: class {
    get isReady() {
      return cosmosReady.isReady
    }
    get ready() {
      return cosmosReady.ready
    }
    setPointPositions(p: Float32Array) {
      calls.setPointPositions += 1
      calls.lastPositions = Float32Array.from(p)
    }
    setPointColors(c: Float32Array) {
      calls.setPointColors += 1
      calls.lastColors = Float32Array.from(c)
    }
    setPointSizes() {
      calls.setPointSizes += 1
    }
    setLinks() {}
    render() {
      calls.render += 1
    }
    setConfigPartial(config: { enableSimulation?: boolean }) {
      if (config.enableSimulation !== undefined) calls.simulationEnabled.push(config.enableSimulation)
    }
    start() {
      calls.start += 1
    }
    pause() {}
    // [13-B] CH2(3) — the readbacks return what was written, not always empty.
    //
    // They used to be `return []` / `return new Map()`: the signatures matched,
    // so every call type-checked and nothing ever failed — but the two pipelines
    // that consume them were invisible to all 288 tests. Deleting the whole body
    // of `capturePositions` (the [7-D] SETTLING→FROZEN readback, including its
    // "NaN must never become a seed anchor" guard) left the suite fully green,
    // and so did stubbing the label overlay to `setLabels([])`.
    //
    // A mock that always answers "nothing" cannot tell a working pipeline from a
    // deleted one. This one echoes: whatever the component last wrote is what it
    // reads back, so a component that stops reading, or reads and discards, is
    // now a visible difference.
    getPointPositions() {
      calls.readbacks += 1
      const source = calls.gpuPositions ?? calls.lastPositions
      return source ? Array.from(source) : []
    }
    destroy() {}
    fitView() {
      calls.fitViews += 1
    }
    trackPointPositionsByIndices(indices: number[]) {
      calls.trackCalls += 1
      calls.trackedIndices = Array.from(indices ?? [])
    }
    getTrackedPointPositionsMap() {
      calls.labelReadbacks += 1
      const flat = calls.gpuPositions ?? calls.lastPositions
      const map = new Map<number, [number, number]>()
      if (!flat) return map
      for (const index of calls.trackedIndices) {
        const x = flat[index * 2]
        const y = flat[index * 2 + 1]
        if (Number.isFinite(x) && Number.isFinite(y)) map.set(index, [x, y])
      }
      return map
    }
    getZoomLevel() {
      return 1
    }
    spaceToScreenPosition(p: [number, number]) {
      return p
    }
  },
}))

const { OverviewCanvas, retainLivePositions } = await import('./OverviewCanvas')
const { buildGraphArrays } = await import('./graphAdapter')
const { useGraphStore } = await import('../stores/graphStore')
const types = await import('../types')
type Node = typeof types extends never ? never : import('../types').Node
type WSEvent = import('../types').WSEvent

let seq = 0

function node(id: string): Node {
  return {
    id,
    label: id,
    type: 'class',
    properties: {},
    parent_id: null,
    style_hint: null,
    position_hint: null,
    layer: null,
    ttl: 0,
    tags: [],
    created_at: 't',
    updated_at: 't',
    created_by: null,
  }
}

function push(...events: WSEvent[]) {
  act(() => {
    for (const e of events) useGraphStore.getState().applyServerEvent(e)
    useGraphStore.getState().flushNow()
  })
}

type BatchData = import('../types').GraphBatchData

function batch(over: Partial<BatchData>) {
  push({
    op: 'graph.batch',
    seq: ++seq,
    data: {
      nodes_added: [],
      nodes_updated: [],
      nodes_deleted: [],
      edges_added: [],
      edges_updated: [],
      edges_deleted: [],
      ...over,
    },
  } as WSEvent)
}

function addNodes(...ids: string[]) {
  batch({ nodes_added: ids.map(node) })
}

function edge(source: string, target: string): import('../types').Edge {
  return {
    source,
    target,
    relation: 'ref',
    key: '',
    directed: true,
    properties: {},
    weight: 1,
    layer: null,
    style_hint: null,
    ttl: 0,
    tags: [],
    created_at: 't',
    created_by: null,
  }
}

/** Positions are flat [x,y,...] in `ids` order, which is node insertion order. */
function positionAt(index: number): [number, number] {
  const p = calls.lastPositions
  if (!p) throw new Error('setPointPositions was never called')
  return [p[index * 2], p[index * 2 + 1]]
}

beforeEach(() => {
  seq = 0
  useGraphStore.getState().reset()
  calls.setPointPositions = 0
  calls.setPointColors = 0
  calls.setPointSizes = 0
  calls.render = 0
  calls.simulationEnabled = []
  calls.start = 0
  calls.lastPositions = null
  calls.trackedIndices = []
  calls.gpuPositions = null
  calls.readbacks = 0
  calls.labelReadbacks = 0
  calls.fitViews = 0
  calls.trackCalls = 0
  ;(window as unknown as { __vbPainted?: unknown }).__vbPainted = undefined
  cosmosReady.isReady = true
  cosmosReady.ready = Promise.resolve()
  vi.useFakeTimers()
})

afterEach(() => {
  vi.useRealTimers()
})

describe('★ (1) highlight-only 재렌더는 레이아웃을 건드리지 않는다', () => {
  test('선택만 바뀌면 settle 을 스케줄하지 않는다', () => {
    const { rerender } = render(<OverviewCanvas highlighted={[]} />)
    addNodes('a', 'b')
    // Let the settle from the real data change run, reaching a laid-out graph.
    act(() => vi.advanceTimersByTime(600))
    expect(calls.simulationEnabled).toContain(true) // sanity: settle does happen
    calls.simulationEnabled = []
    calls.setPointPositions = 0
    calls.start = 0 // the settle above is legitimate; only what follows is under test

    // The user clicks a finding: anchors highlight, nothing moves.
    rerender(<OverviewCanvas highlighted={['a']} />)
    act(() => vi.advanceTimersByTime(2000))

    // The graph was settled; selecting must not wake the simulation back up.
    expect(calls.simulationEnabled).not.toContain(true)
    expect(calls.start).toBe(0)
  })

  test('선택만 바뀌면 위치를 다시 쓰지 않는다', () => {
    const { rerender } = render(<OverviewCanvas highlighted={[]} />)
    addNodes('a', 'b')
    act(() => vi.advanceTimersByTime(600))
    calls.setPointPositions = 0

    rerender(<OverviewCanvas highlighted={['a']} />)

    // setPointPositions is what costs ~130ms and re-seeds the layout.
    expect(calls.setPointPositions).toBe(0)
  })

  test('그래도 오버레이(색/크기)는 갱신된다', () => {
    const { rerender } = render(<OverviewCanvas highlighted={[]} />)
    addNodes('a', 'b')
    act(() => vi.advanceTimersByTime(600))
    calls.setPointColors = 0
    calls.setPointSizes = 0

    rerender(<OverviewCanvas highlighted={['a']} />)

    // Skipping the data path must not mean skipping the highlight itself.
    expect(calls.setPointColors).toBeGreaterThan(0)
    expect(calls.setPointSizes).toBeGreaterThan(0)
  })

  test('실제 데이터 변경은 여전히 전체 경로를 탄다', () => {
    render(<OverviewCanvas highlighted={[]} />)
    addNodes('a')
    calls.setPointPositions = 0
    calls.simulationEnabled = []

    addNodes('b')
    act(() => vi.advanceTimersByTime(600))

    expect(calls.setPointPositions).toBeGreaterThan(0)
    expect(calls.simulationEnabled).toContain(true) // a settle is armed again
  })
})

describe('★ (B) settle 은 구조 변경(node/edge add·remove)에만 무장한다', () => {
  /** Reach a settled graph, then watch only what the next event does. */
  function settledWith(...ids: string[]) {
    const view = render(<OverviewCanvas highlighted={[]} />)
    addNodes(...ids)
    act(() => vi.advanceTimersByTime(600))
    expect(calls.simulationEnabled).toContain(true) // sanity: a settle did happen
    calls.simulationEnabled = []
    calls.start = 0
    calls.setPointPositions = 0
    calls.setPointColors = 0
    return view
  }

  test('finding 기록은 그래프를 흔들지 않는다 — 이 툴의 핵심 행위다', () => {
    settledWith('a', 'b')

    push({
      op: 'finding.add',
      seq: ++seq,
      data: {
        finding_id: 'f1',
        title: 'gold',
        body: '',
        node_ids: ['a'],
        confidence: 0.9,
        evidence: [],
        layer: null,
        tags: [],
        created_by: null,
        created_at: 't',
        updated_at: 't',
      },
    } as WSEvent)
    act(() => vi.advanceTimersByTime(3000))

    // Recording gold used to re-seed and settle: ~2.8s of lurching every time.
    expect(calls.setPointPositions).toBe(0)
    expect(calls.simulationEnabled).not.toContain(true)
    expect(calls.start).toBe(0)
  })

  test('property-only 갱신은 settle 을 무장하지 않는다', () => {
    settledWith('a', 'b')

    batch({ nodes_updated: [{ id: 'a', patch: { set: { properties: { note: 'x' } } } }] })
    act(() => vi.advanceTimersByTime(3000))

    expect(calls.setPointPositions).toBe(0)
    expect(calls.simulationEnabled).not.toContain(true)
    expect(calls.start).toBe(0)
  })

  test('★ property-only 갱신이어도 색은 갱신된다 (색 회귀 금지)', () => {
    settledWith('a', 'b')

    // type decides the palette colour ([7-A]), and update_node can change it.
    batch({ nodes_updated: [{ id: 'a', patch: { set: { type: 'function' } } }] })

    expect(calls.setPointColors).toBeGreaterThan(0)
  })

  test('node 추가는 여전히 settle 을 무장한다', () => {
    settledWith('a')

    addNodes('b')
    act(() => vi.advanceTimersByTime(600))

    expect(calls.setPointPositions).toBeGreaterThan(0)
    expect(calls.simulationEnabled).toContain(true)
  })

  test('edge 추가는 여전히 settle 을 무장한다 (위상이 바뀐다)', () => {
    settledWith('a', 'b')

    batch({ edges_added: [edge('a', 'b')] })
    act(() => vi.advanceTimersByTime(600))

    expect(calls.simulationEnabled).toContain(true)
  })

  test('node 삭제는 여전히 settle 을 무장한다', () => {
    settledWith('a', 'b')

    batch({ nodes_deleted: [{ id: 'b' }] })
    act(() => vi.advanceTimersByTime(600))

    expect(calls.simulationEnabled).toContain(true)
  })

  test('같은 flush 의 1삭제+1추가는 카운트가 그대로여도 구조 변경이다', () => {
    settledWith('a', 'b')

    // nodeCount stays 2 — a count-delta gate would miss this, and the new node
    // would never get seeded.
    batch({ nodes_deleted: [{ id: 'b' }], nodes_added: [node('c')] })
    act(() => vi.advanceTimersByTime(600))

    expect(calls.setPointPositions).toBeGreaterThan(0)
    expect(calls.simulationEnabled).toContain(true)
  })
})

describe('★ (A) 삭제된 노드의 좌표가 call-site 에 남지 않는다 (통합)', () => {
  /**
   * Goes through the rendered component and the real store delete path, not
   * retainLivePositions() directly.
   *
   * The unit test alone let a mutant live: reverting the call-site to an in-place
   * `positions.current.set(...)` loop kept every test green, because the pure
   * function was still correct — nobody checked that the component used it. This
   * asserts the consequence instead: a node that was deleted and pushed again is
   * a *new* node, so it must be seeded beside the neighbour it now connects to
   * ([7-D] A). If the call-site still holds the dead coordinate, `previous('b')`
   * returns it and b silently reappears where it used to be.
   */
  test('삭제 후 재추가된 노드는 옛 좌표가 아니라 새 이웃 옆에 시드된다', () => {
    render(<OverviewCanvas highlighted={[]} />)
    addNodes('a', 'b')
    const bBefore = positionAt(1) // ids = [a, b]

    batch({ nodes_deleted: [{ id: 'b' }] })
    // b comes back, now connected to a — its seed must follow the edge.
    batch({ nodes_added: [node('b')], edges_added: [edge('b', 'a')] })

    const aPos = positionAt(0)
    const bAfter = positionAt(1)

    // Seeded beside its neighbour: within the deterministic jitter box
    // (NEIGHBOUR_JITTER = 1% of the 4096 space → ±20.48 per axis).
    expect(Math.abs(bAfter[0] - aPos[0])).toBeLessThanOrEqual(21)
    expect(Math.abs(bAfter[1] - aPos[1])).toBeLessThanOrEqual(21)
    // And demonstrably not the position it had before it was deleted.
    expect(bAfter).not.toEqual(bBefore)
  })

  test('살아있는 노드는 삭제 이벤트 후에도 제자리를 지킨다', () => {
    render(<OverviewCanvas highlighted={[]} />)
    addNodes('a', 'b')
    const aBefore = positionAt(0)

    batch({ nodes_deleted: [{ id: 'b' }] })

    // Deleting a neighbour must not shuffle the survivors.
    expect(positionAt(0)).toEqual(aBefore)
  })
})

describe('(2) retainLivePositions 순수함수', () => {
  const nodes = (ids: string[]) => new Map(ids.map((id) => [id, node(id)]))

  test('삭제된 노드는 남지 않는다', () => {
    const before = retainLivePositions(buildGraphArrays(nodes(['a', 'b']), new Map()))
    expect([...before.keys()].sort()).toEqual(['a', 'b'])

    // 'b' deleted: its position must go with it, not linger for the session.
    const after = retainLivePositions(buildGraphArrays(nodes(['a']), new Map()))
    expect([...after.keys()]).toEqual(['a'])
    expect(after.has('b')).toBe(false)
  })

  test('살아있는 노드의 좌표는 보존된다', () => {
    const built = buildGraphArrays(nodes(['a', 'b']), new Map())
    const live = retainLivePositions(built)

    expect(live.get('a')).toEqual([built.pointPositions[0], built.pointPositions[1]])
  })

  test('NaN 좌표는 저장하지 않는다 (absent point 는 seed 앵커가 될 수 없다)', () => {
    const built = buildGraphArrays(nodes(['a', 'b']), new Map())
    built.pointPositions[0] = Number.NaN

    const live = retainLivePositions(built)

    expect(live.has('a')).toBe(false)
    expect(live.has('b')).toBe(true)
  })
})

describe('★ (V) 필터 변경은 오버레이 경로다 (settle 미무장·structureSeq 불변)', () => {
  function filterSet(expression: string, visibleIds: string[]) {
    push({
      op: 'filter.set',
      seq: ++seq,
      data: { expression, visible_ids: visibleIds, error: null },
    } as WSEvent)
  }

  test('필터 적용은 위치를 다시 쓰지 않고 settle 을 무장하지 않는다', () => {
    render(<OverviewCanvas highlighted={[]} />)
    addNodes('a', 'b')
    act(() => vi.advanceTimersByTime(600)) // let the structural settle finish
    calls.setPointPositions = 0
    calls.simulationEnabled = []
    calls.start = 0

    filterSet('type == "class"', ['a'])
    act(() => vi.advanceTimersByTime(2000))

    // Filtering is a colour overlay: no re-seed (setPointPositions), no settle.
    expect(calls.setPointPositions).toBe(0)
    expect(calls.simulationEnabled).not.toContain(true)
    expect(calls.start).toBe(0)
  })

  test('필터 적용은 색을 갱신한다 (dim 오버레이)', () => {
    render(<OverviewCanvas highlighted={[]} />)
    addNodes('a', 'b')
    act(() => vi.advanceTimersByTime(600))
    calls.setPointColors = 0

    filterSet('type == "class"', ['a'])

    expect(calls.setPointColors).toBeGreaterThan(0)
  })

  test('필터 변경은 structureSeq 를 바꾸지 않는다', () => {
    addNodes('a', 'b')
    const before = useGraphStore.getState().structureSeq

    filterSet('type == "class"', ['a'])

    // ★ Mutation guard: if filter.set were routed through the structural path
    // (bumping structureSeq), the overview would re-seed and settle — the exact
    // ~2.8s lurch this must avoid. structureSeq must stay put.
    expect(useGraphStore.getState().structureSeq).toBe(before)
  })
})

describe('★ (Y) AI style is an overlay (no reseed/settle), structureSeq unchanged', () => {
  function styleApply(styleId: string, ids: string[], style: object, ttl = 0) {
    push({
      op: 'style.apply',
      seq: ++seq,
      data: { style_id: styleId, ids, style, ttl },
    } as WSEvent)
  }

  test('applying a style does not reseed or settle', () => {
    render(<OverviewCanvas highlighted={[]} />)
    addNodes('a', 'b')
    act(() => vi.advanceTimersByTime(600))
    calls.setPointPositions = 0
    calls.simulationEnabled = []
    calls.start = 0

    styleApply('s1', ['a'], { color: '#ff0000' })
    act(() => vi.advanceTimersByTime(2000))

    expect(calls.setPointPositions).toBe(0)
    expect(calls.simulationEnabled).not.toContain(true)
    expect(calls.start).toBe(0)
  })

  test('applying a style updates colours', () => {
    render(<OverviewCanvas highlighted={[]} />)
    addNodes('a', 'b')
    act(() => vi.advanceTimersByTime(600))
    calls.setPointColors = 0

    styleApply('s1', ['a'], { color: '#ff0000' })

    expect(calls.setPointColors).toBeGreaterThan(0)
  })

  test('★ style.apply does not bump structureSeq (mutation guard)', () => {
    addNodes('a', 'b')
    const before = useGraphStore.getState().structureSeq

    styleApply('s1', ['a'], { color: '#ff0000' })

    expect(useGraphStore.getState().structureSeq).toBe(before)
  })

  test('ttl expiry removes the style', () => {
    styleApply('s1', ['a'], { color: '#ff0000' }, 2)
    expect(useGraphStore.getState().aiStyles.has('s1')).toBe(true)

    act(() => vi.advanceTimersByTime(2000))

    expect(useGraphStore.getState().aiStyles.has('s1')).toBe(false)
  })
})

describe('★ [KI-1] cosmos readiness gate — 미준비 상태를 시뮬레이션', () => {
  test('cosmos 가 ready 전에 도착한 데이터는 setPointPositions 를 호출하지 않는다 (크래시 방지)', async () => {
    // The load race: cosmos exists but its WebGL device/points are not built yet.
    let resolveReady!: () => void
    cosmosReady.isReady = false
    cosmosReady.ready = new Promise<void>((resolve) => {
      resolveReady = () => {
        cosmosReady.isReady = true
        resolve()
      }
    })

    render(<OverviewCanvas highlighted={[]} />)
    addNodes('a', 'b') // first structural push while cosmos is still initialising

    // setPointPositions is the exact call that throws pre-ready in cosmos 3.3.0.
    // The gate must hold it back — deterministically, not by hoping init wins.
    expect(calls.setPointPositions).toBe(0)

    // cosmos signals ready → the parked apply runs against the current graph.
    await act(async () => {
      resolveReady()
      await cosmosReady.ready
    })

    expect(calls.setPointPositions).toBeGreaterThan(0)
    expect(calls.lastPositions).not.toBeNull()
  })

  test('ready 상태면 게이트가 정상 경로를 막지 않는다', () => {
    // cosmosReady defaults to ready (beforeEach) — the common load path.
    render(<OverviewCanvas highlighted={[]} />)
    addNodes('a')

    expect(calls.setPointPositions).toBeGreaterThan(0)
  })
})


describe('★ [13-B] CH2(3) GPU readback — mock 이 값을 되돌려주므로 관측된다', () => {
  test('settle 이 끝나면 GPU 에서 배치를 실제로 읽어온다', () => {
    render(<OverviewCanvas highlighted={[]} />)
    addNodes('a', 'b')

    // 시뮬레이션이 좌표를 움직인 것을 흉내낸다 — 업로드한 값과 **다른** 값을
    // 돌려주는 것이 readback 의 요점이다.
    const seeded = calls.lastPositions
    expect(seeded).not.toBeNull()
    const moved = Float32Array.from(seeded as Float32Array)
    for (let i = 0; i < moved.length; i += 1) moved[i] += 100
    calls.gpuPositions = moved

    act(() => vi.advanceTimersByTime(600))
    act(() => vi.advanceTimersByTime(4000)) // settle 상한까지 — FROZEN 에서 capture

    // ★ 이 단언이 capturePositions 본문 삭제를 잡는다. 이전 mock 은 항상 빈 값을
    // 돌려줬으므로 그 본문을 통째로 지워도 288/288 초록이었다 — [7-D]
    // SETTLING→FROZEN 의 배치 보존과 "NaN 은 seed 앵커가 되면 안 된다" 가드가
    // 함께 사라져도 아무 테스트도 몰랐다.
    expect(calls.readbacks).toBeGreaterThan(0)
  })

  test('NaN 좌표는 seed 앵커로 저장되지 않는다', () => {
    // capturePositions 와 retainLivePositions 가 공유하는 불변식. 없는 점은
    // cosmos 가 설계상 NaN 으로 돌려주므로, 삭제된 노드가 이웃의 seed 앵커로
    // 되살아나면 안 된다.
    const built = {
      ids: ['a', 'b', 'c'],
      pointPositions: Float32Array.from([1, 2, Number.NaN, 5, 7, Number.NaN]),
    } as unknown as Parameters<typeof retainLivePositions>[0]

    const live = retainLivePositions(built)
    expect([...live.keys()]).toEqual(['a'])
    expect(live.get('a')).toEqual([1, 2])
  })

  test('라벨 오버레이가 GPU 읽기 파이프라인을 실제로 돈다', () => {
    render(<OverviewCanvas highlighted={[]} />)
    addNodes('a', 'b')
    act(() => vi.advanceTimersByTime(600))
    act(() => vi.advanceTimersByTime(4000)) // 라벨 갱신은 스로틀돼 있다

    // 이전 mock 은 항상 빈 Map 을 돌려줬으므로 이 파이프라인이 도는지 여부가
    // 288건 어디에도 나타나지 않았다. 이제는 호출 자체가 관측된다.
    expect(calls.labelReadbacks).toBeGreaterThan(0)
    expect(calls.trackedIndices.length).toBeGreaterThan(0)

    // ★ 여기까지가 vitest 에서 정직하게 볼 수 있는 범위다. 라벨이 실제로 DOM 에
    // **놓이는지**는 jsdom 에서 확인할 수 없다 — getBoundingClientRect 가 전부 0
    // 이라 화면 안/밖 판정이 성립하지 않는다. 그 축은 실제 브라우저가 필요하고
    // frontend/e2e/graph.spec.ts 가 담당한다.
  })
})

/**
 * ★ [15] (5) push→표시 — 종료점 고정과 두 레버 (계획서 [15 개정], TASK PERF1).
 *
 * 이 블록이 지키는 것은 "빨라졌다" 가 아니라 **어디까지가 push 경로인가** 다.
 * 시간은 이 환경(jsdom + mock)에서 아무 의미가 없고, 의미가 있는 것은 push 1회가
 * 무엇을 부르고 무엇을 부르지 않느냐다. M3c 회귀가 3주간 숨었던 이유도 정확히
 * 이것을 세는 단언이 없어서였다.
 */
describe('★ [15] (5) push→표시: 종료점과 push 경로', () => {
  test('[1] 데이터가 캔버스에 올라간 시점을 코드가 직접 찍는다', () => {
    const painted = () => (window as unknown as { __vbPainted?: { n: number; at: number } }).__vbPainted
    render(<OverviewCanvas highlighted={[]} />)

    // 마운트도 한 번 적용한다(빈 그래프). harness 가 시각이 아니라 **카운터
    // 증가**로 판정하는 이유가 이것이다 — 자기 push 이전의 페인트를 새 것으로
    // 착각하지 않는다.
    const base = painted()?.n ?? 0
    addNodes('a', 'b')

    // ★ harness 의 KPI 종료점이 바로 이 마크다. 없으면 (5)는 다시 React 스케줄링
    // 우연에 기대는 DOM 관측으로 돌아가고, 그게 73.9ms 를 만든 사고였다.
    expect(painted()?.n, 'push 가 페인트 마크를 올리지 않았다').toBe(base + 1)
    const first = painted()!.at
    expect(Number.isFinite(first)).toBe(true)

    // 시각이 실제로 그때그때 찍히는지 — 가짜 타이머로 시간을 밀고 다시 push 한다.
    // (절대값은 이 환경에서 의미가 없다. 실제 수치는 perf harness 가 잰다.)
    act(() => vi.advanceTimersByTime(500))
    addNodes('c')
    expect(painted()!.n, '두 번째 push 도 세어져야 harness 가 자기 push 를 구분한다').toBe(base + 2)
    expect(painted()!.at, '시각이 갱신되지 않으면 두 번째 측정이 첫 번째를 다시 잰다')
      .toBeGreaterThan(first)
  })

  test('[3a] tracking 은 push 경로에서 빠지고 라벨 주기로 옮겨간다', () => {
    render(<OverviewCanvas highlighted={[]} />)
    addNodes('a', 'b')
    act(() => vi.advanceTimersByTime(600))
    act(() => vi.advanceTimersByTime(4000)) // 라벨 스로틀을 지나 tracking 이 한 번 돈다
    calls.trackCalls = 0

    addNodes('c') // 조용한 구간 뒤 단발 push — (5)가 재는 바로 그 모양

    // push 그 자리에서는 tracking 이 없다(16.0ms 가 여기 있었다).
    expect(calls.trackCalls, 'tracking 이 여전히 push 경로에 있다').toBe(0)

    // 그래도 결국은 갱신된다 — 라벨이 새 노드를 못 받으면 기능이 죽는다.
    act(() => vi.advanceTimersByTime(300))
    expect(calls.trackCalls, '라벨 주기에서도 tracking 이 일어나지 않았다').toBeGreaterThan(0)
    expect(calls.trackedIndices.length).toBeGreaterThan(0)
  })

  test('[3b] 재프레이밍은 push 경로에서 빠지되 곧 따라온다', () => {
    render(<OverviewCanvas highlighted={[]} />)
    addNodes('a', 'b')
    act(() => vi.advanceTimersByTime(3000)) // settle 까지 끝낸 조용한 상태
    calls.fitViews = 0

    addNodes('c')

    // push 그 자리에서는 fitView 가 없다(17.5ms 가 여기 있었다).
    expect(calls.fitViews, 'keepFramed 가 여전히 push 경로에 있다').toBe(0)

    // ★ 그러나 카메라는 반드시 따라온다. 안 그러면 화면 밖에 놓인 새 노드를
    // 사용자가 영영 못 본다 — 그건 성능이 아니라 기능 파손이다.
    act(() => vi.advanceTimersByTime(150))
    expect(calls.fitViews, '새 노드가 온 뒤에도 카메라가 따라오지 않았다').toBeGreaterThan(0)
  })

  test('[3b] 버스트가 길어도 프레이밍이 굶지 않는다 (디바운스가 아니라 스로틀)', () => {
    render(<OverviewCanvas highlighted={[]} />)
    addNodes('seed')
    act(() => vi.advanceTimersByTime(3000))
    calls.fitViews = 0

    // 100ms 간격보다 촘촘하게 계속 밀어넣는다.
    for (let i = 0; i < 12; i += 1) {
      addNodes(`burst${i}`)
      act(() => vi.advanceTimersByTime(40))
    }

    // 디바운스였다면 여기서 0 이다 — 매 push 가 마감을 미루므로.
    expect(calls.fitViews, '버스트 내내 카메라가 한 번도 따라오지 않았다').toBeGreaterThan(0)
  })

  test('[3b] settle 이 끝나면 프레이밍이 반드시 맞춰진다', () => {
    render(<OverviewCanvas highlighted={[]} />)
    addNodes('a', 'b')
    act(() => vi.advanceTimersByTime(600)) // settle 진입
    calls.fitViews = 0

    act(() => vi.advanceTimersByTime(2500)) // SETTLE_MAX_MS 를 지나 finishSettle

    // 레이아웃이 최종 위치로 움직인 뒤의 프레이밍은 디바운스에 맡기지 않는다.
    expect(calls.fitViews, 'settle 종료 시 프레이밍이 없었다').toBeGreaterThan(0)
    expect(calls.simulationEnabled).toContain(false) // FROZEN 도달
  })

  test('[7-D] 두 레버가 상태기계를 건드리지 않는다', () => {
    const { rerender } = render(<OverviewCanvas highlighted={[]} />)
    addNodes('a', 'b')
    act(() => vi.advanceTimersByTime(3000)) // INGESTING → SETTLING → FROZEN
    calls.simulationEnabled = []
    calls.start = 0
    calls.setPointPositions = 0

    // highlight-only 는 여전히 레이아웃을 깨우지 않는다(structureSeq 가드).
    rerender(<OverviewCanvas highlighted={['a']} />)
    act(() => vi.advanceTimersByTime(2000))
    expect(calls.simulationEnabled).not.toContain(true)
    expect(calls.start).toBe(0)
    expect(calls.setPointPositions).toBe(0)

    // 반대로 구조 변경은 여전히 settle 을 재무장한다.
    addNodes('c')
    act(() => vi.advanceTimersByTime(600))
    expect(calls.simulationEnabled).toContain(true)
  })
})
