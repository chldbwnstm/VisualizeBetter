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
    getPointPositions() {
      return []
    }
    destroy() {}
    fitView() {}
    trackPointPositionsByIndices() {}
    getTrackedPointPositionsMap() {
      return new Map()
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
