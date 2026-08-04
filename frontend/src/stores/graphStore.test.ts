/**
 * Completion verification for TASK 7a — store ([9-C]) + [7-D] batching.
 */

import { beforeEach, describe, expect, test } from 'vitest'
import { graphData, timelineBoundsNow, useGraphStore } from './graphStore'
import { temporalCounters, timelineBounds } from '../views/temporal'
import type { Edge, Finding, GraphBatchData, Node, WSEvent } from '../types'
import { edgeKey } from '../types'

let seq = 0

function node(id: string, over: Partial<Node> = {}): Node {
  return {
    id,
    label: id.toUpperCase(),
    type: 'class',
    properties: {},
    parent_id: null,
    style_hint: null,
    position_hint: null,
    layer: null,
    ttl: 0,
    tags: [],
    created_at: '2026-07-17T00:00:00+00:00',
    updated_at: '2026-07-17T00:00:00+00:00',
    created_by: null,
    ...over,
  }
}

function edge(source: string, target: string, over: Partial<Edge> = {}): Edge {
  return {
    source,
    target,
    relation: 'field',
    key: '',
    directed: true,
    properties: {},
    weight: 1,
    layer: null,
    style_hint: null,
    ttl: 0,
    tags: [],
    created_at: '2026-07-17T00:00:00+00:00',
    created_by: null,
    ...over,
  }
}

function finding(id: string, over: Partial<Finding> = {}): Finding {
  return {
    finding_id: id,
    title: 'gold',
    body: '',
    node_ids: [],
    confidence: 0.8,
    evidence: [],
    layer: null,
    tags: [],
    created_by: null,
    created_at: '2026-07-17T00:00:00+00:00',
    updated_at: '2026-07-17T00:00:00+00:00',
    ...over,
  }
}

function emptyBatch(): GraphBatchData {
  return {
    nodes_added: [],
    nodes_updated: [],
    nodes_deleted: [],
    edges_added: [],
    edges_updated: [],
    edges_deleted: [],
  }
}

/** Feed events and flush, the way the rAF window would. */
function apply(...events: WSEvent[]) {
  const store = useGraphStore.getState()
  for (const e of events) store.applyServerEvent(e)
  useGraphStore.getState().flushNow()
}

function batch(over: Partial<GraphBatchData>): WSEvent {
  return { op: 'graph.batch', data: { ...emptyBatch(), ...over }, seq: ++seq }
}

beforeEach(() => {
  seq = 0
  useGraphStore.getState().reset()
})

describe('graph.batch ([8-C])', () => {
  test('nodes_added lands in off-React storage, store keeps the count', () => {
    apply(batch({ nodes_added: [node('a'), node('b')] }))

    expect(graphData.nodeCount()).toBe(2)
    expect(graphData.getNode('a')?.label).toBe('A')
    expect(useGraphStore.getState().nodeCount).toBe(2)
  })

  test('bulk node bodies are not in the store ([7-D])', () => {
    apply(batch({ nodes_added: [node('a')] }))

    expect(useGraphStore.getState()).not.toHaveProperty('nodes')
    expect(useGraphStore.getState()).not.toHaveProperty('edges')
  })

  test('edges are keyed by the 4-tuple ([9-C])', () => {
    apply(batch({ edges_added: [edge('a', 'b', { key: 'm_health' })] }))

    expect(graphData.getEdge(edgeKey('a', 'b', 'field', 'm_health'))).toBeDefined()
    expect(useGraphStore.getState().edgeCount).toBe(1)
  })

  test('parallel edges coexist under distinct keys', () => {
    apply(
      batch({
        edges_added: [
          edge('a', 'b', { key: 'm_health' }),
          edge('a', 'b', { key: 'm_mana' }),
          edge('a', 'b', { relation: 'call' }),
        ],
      }),
    )

    expect(graphData.edgeCount()).toBe(3)
  })

  test('nodes_updated applies a [5-A] patch', () => {
    apply(batch({ nodes_added: [node('a', { properties: { ns: 'MOD', old: 1 } })] }))
    apply(
      batch({
        nodes_updated: [
          { id: 'a', patch: { set: { label: 'Renamed', properties: { x: 2 } }, remove: ['old'] } },
        ],
      }),
    )

    const updated = graphData.getNode('a')
    expect(updated?.label).toBe('Renamed')
    expect(updated?.properties).toEqual({ ns: 'MOD', x: 2 })
  })

  test('nodes_deleted removes and drops the count', () => {
    apply(batch({ nodes_added: [node('a'), node('b')] }))
    apply(batch({ nodes_deleted: [{ id: 'a' }] }))

    expect(graphData.getNode('a')).toBeUndefined()
    expect(useGraphStore.getState().nodeCount).toBe(1)
  })

  test('edges_updated and edges_deleted resolve by 4-tuple', () => {
    apply(batch({ edges_added: [edge('a', 'b', { key: 'k' })] }))
    apply(
      batch({
        edges_updated: [
          { source: 'a', target: 'b', relation: 'field', key: 'k', patch: { set: { weight: 0.3 } } },
        ],
      }),
    )
    expect(graphData.getEdge(edgeKey('a', 'b', 'field', 'k'))?.weight).toBe(0.3)

    apply(batch({ edges_deleted: [{ source: 'a', target: 'b', relation: 'field', key: 'k' }] }))
    expect(graphData.edgeCount()).toBe(0)
  })

  test('an update for an unknown node is ignored, not invented', () => {
    apply(batch({ nodes_updated: [{ id: 'ghost', patch: { set: { label: 'X' } } }] }))

    expect(graphData.getNode('ghost')).toBeUndefined()
  })

  test('layers seen on nodes are registered for LayerToggleList ([9-B])', () => {
    apply(batch({ nodes_added: [node('a', { layer: 'claude-1' })] }))

    expect(useGraphStore.getState().layers.get('claude-1')).toEqual({
      visible: true,
      color: null,
    })
  })
})

describe('[7-D] batching', () => {
  test('applyServerEvent does not write the store until flush', () => {
    useGraphStore.getState().applyServerEvent(batch({ nodes_added: [node('a')] }))

    expect(useGraphStore.getState().nodeCount).toBe(0)
    expect(graphData.nodeCount()).toBe(0)

    useGraphStore.getState().flushNow()
    expect(useGraphStore.getState().nodeCount).toBe(1)
  })

  test('many events collapse into one store write', () => {
    let writes = 0
    const unsub = useGraphStore.subscribe(() => {
      writes += 1
    })

    const store = useGraphStore.getState()
    for (let i = 0; i < 50; i += 1) {
      store.applyServerEvent(batch({ nodes_added: [node(`n${i}`)] }))
    }
    useGraphStore.getState().flushNow()
    unsub()

    expect(graphData.nodeCount()).toBe(50)
    expect(writes).toBe(1)
  })

  test('flushing an empty queue writes nothing', () => {
    let writes = 0
    const unsub = useGraphStore.subscribe(() => {
      writes += 1
    })
    useGraphStore.getState().flushNow()
    unsub()

    expect(writes).toBe(0)
  })

  test('a second flush re-applies nothing', () => {
    apply(batch({ nodes_added: [node('a')] }))
    useGraphStore.getState().flushNow()

    expect(graphData.nodeCount()).toBe(1)
  })

  test('the rAF window flushes without an explicit call', async () => {
    useGraphStore.getState().applyServerEvent(batch({ nodes_added: [node('a')] }))

    await new Promise((resolve) => setTimeout(resolve, 80))

    expect(useGraphStore.getState().nodeCount).toBe(1)
  })

  test('highest seq is tracked for resync ([8-C])', () => {
    apply(batch({ nodes_added: [node('a')] }), batch({ nodes_added: [node('b')] }))

    expect(useGraphStore.getState().seq).toBe(2)
  })
})

describe('[7-D] structureSeq — attribute changes must not reseed the overview', () => {
  // ★ mutation guard for undo/redo (M2g #2): undo of an attribute-only change now
  // arrives as node.update, which must leave structureSeq put — otherwise the
  // overview takes the structural reseed path (~2.2s settle). node.add/delete
  // still bump it (contrast below), proving the guard actually discriminates.
  test('a standalone node.update leaves structureSeq unchanged; node.add bumps it', () => {
    apply(batch({ nodes_added: [node('a')] }))
    const s0 = useGraphStore.getState().structureSeq

    apply({ op: 'node.update', seq: ++seq, data: { id: 'a', patch: { set: { label: 'renamed' } } } } as WSEvent)
    expect(useGraphStore.getState().structureSeq).toBe(s0) // no reseed
    expect(graphData.getNode('a')?.label).toBe('renamed') // still applied

    apply(batch({ nodes_added: [node('b')] }))
    expect(useGraphStore.getState().structureSeq).toBe(s0 + 1) // structural, reseeds
  })

  test('a graph.batch carrying only nodes_updated / edges_updated is non-structural', () => {
    apply(batch({ nodes_added: [node('a')], edges_added: [edge('a', 'a')] }))
    const s0 = useGraphStore.getState().structureSeq
    apply(
      batch({
        nodes_updated: [{ id: 'a', patch: { set: { label: 'x' } } }],
        edges_updated: [{ source: 'a', target: 'a', relation: 'field', key: '', patch: { set: { weight: 2 } } }],
      }),
    )
    expect(useGraphStore.getState().structureSeq).toBe(s0)
  })

  // ★ [M3c] the temporal scrubber is a pure overlay: moving the cutoff must never
  // bump structureSeq, or every scrub tick would reseed + settle the 100K overview
  // (the exact structural cost M3a/M3b removed). Contrast: node.add still bumps it.
  test('setTemporalCutoff does not bump structureSeq (scrub stays on the overlay path)', () => {
    apply(batch({ nodes_added: [node('a')] }))
    const s0 = useGraphStore.getState().structureSeq
    useGraphStore.getState().setTemporalCutoff(1000)
    expect(useGraphStore.getState().temporalCutoff).toBe(1000)
    expect(useGraphStore.getState().structureSeq).toBe(s0) // scrub is not structural
    useGraphStore.getState().setTemporalCutoff(null) // back to live
    expect(useGraphStore.getState().temporalCutoff).toBeNull()
    expect(useGraphStore.getState().structureSeq).toBe(s0)
  })
})

describe('finding.* ([8-C], not coalesced)', () => {
  test('finding.add stores gold reactively', () => {
    apply({ op: 'finding.add', data: finding('f1', { title: '결제 실패 경로' }), seq: ++seq })

    expect(useGraphStore.getState().findings.get('f1')?.title).toBe('결제 실패 경로')
  })

  test('finding.update merges the patch', () => {
    apply({ op: 'finding.add', data: finding('f1'), seq: ++seq })
    apply({
      op: 'finding.update',
      data: { finding_id: 'f1', patch: { set: { confidence: 0.2, node_ids: ['a'] } } },
      seq: ++seq,
    })

    const updated = useGraphStore.getState().findings.get('f1')
    expect(updated?.confidence).toBe(0.2)
    expect(updated?.node_ids).toEqual(['a'])
    expect(updated?.title).toBe('gold')
  })

  test('finding.delete removes it', () => {
    apply({ op: 'finding.add', data: finding('f1'), seq: ++seq })
    apply({ op: 'finding.delete', data: { finding_id: 'f1' }, seq: ++seq })

    expect(useGraphStore.getState().findings.size).toBe(0)
  })

  test('findings never enter the graph topology ([23-B])', () => {
    apply({ op: 'finding.add', data: finding('f1', { node_ids: ['a'] }), seq: ++seq })

    expect(graphData.nodeCount()).toBe(0)
    expect(graphData.edgeCount()).toBe(0)
  })

  test('an update for an unknown finding is ignored', () => {
    apply({
      op: 'finding.update',
      data: { finding_id: 'ghost', patch: { set: { title: 'X' } } },
      seq: ++seq,
    })

    expect(useGraphStore.getState().findings.size).toBe(0)
  })
})

describe('view ops ([8-C])', () => {
  test('focus.set updates focus', () => {
    apply({ op: 'focus.set', data: { id: 'a' }, seq: ++seq })

    expect(useGraphStore.getState().focus).toBe('a')
  })

  test('filter.set stores expression and visibleIds', () => {
    apply({
      op: 'filter.set',
      data: { expression: 'type == "class"', visible_ids: ['a', 'b'] },
      seq: ++seq,
    })

    const { filter } = useGraphStore.getState()
    expect(filter.expression).toBe('type == "class"')
    expect(filter.visibleIds).toEqual(new Set(['a', 'b']))
  })

  test('layer.toggle applies the server-derived visible flag', () => {
    apply({ op: 'layer.toggle', data: { layer: 'l1', visible: false }, seq: ++seq })

    expect(useGraphStore.getState().layers.get('l1')?.visible).toBe(false)
  })

  test('layer.toggle keeps the layer colour', () => {
    apply(batch({ nodes_added: [node('a', { layer: 'l1' })] }))
    const layers = new Map(useGraphStore.getState().layers)
    layers.set('l1', { visible: true, color: '#f00' })
    useGraphStore.setState({ layers })

    apply({ op: 'layer.toggle', data: { layer: 'l1', visible: false }, seq: ++seq })

    expect(useGraphStore.getState().layers.get('l1')).toEqual({
      visible: false,
      color: '#f00',
    })
  })

  test('[5-D] style.apply and style.clear live in reactive store state', () => {
    apply({
      op: 'style.apply',
      data: { style_id: 's1', ids: ['a', 'b'], style: { color: '#0f0' }, ttl: 0 },
      seq: ++seq,
    })
    expect(useGraphStore.getState().aiStyles.get('s1')).toEqual({
      ids: ['a', 'b'],
      style: { color: '#0f0' },
    })

    apply({ op: 'style.clear', data: { style_id: 's1' }, seq: ++seq })
    expect(useGraphStore.getState().aiStyles.size).toBe(0)
  })

  test('[5-D] style.clear without an id drops every style', () => {
    apply({
      op: 'style.apply',
      data: { style_id: 's1', ids: ['a'], style: { size: 20 }, ttl: 0 },
      seq: ++seq,
    })
    apply({ op: 'style.clear', data: {}, seq: ++seq })

    expect(useGraphStore.getState().aiStyles.size).toBe(0)
  })

  test('[5-D] annotation.add lands in store state', () => {
    apply({
      op: 'annotation.add',
      data: { annotation_id: 'n1', x: 10, y: 20, text: 'look', ttl: 0 },
      seq: ++seq,
    })
    expect(useGraphStore.getState().annotations.get('n1')).toEqual({ x: 10, y: 20, text: 'look' })
  })

  test('unhandled ops do not break the stream', () => {
    apply(
      { op: 'snapshot.load', data: { snapshot_id: 's' }, seq: ++seq },
      batch({ nodes_added: [node('a')] }),
    )

    expect(graphData.nodeCount()).toBe(1)
  })
})

describe('clear ([8-C])', () => {
  test('clear without a layer empties the graph but keeps the gold', () => {
    apply(batch({ nodes_added: [node('a')], edges_added: [edge('a', 'b')] }))
    apply({ op: 'finding.add', data: finding('f1'), seq: ++seq })

    apply({ op: 'clear', data: {}, seq: ++seq })

    expect(graphData.nodeCount()).toBe(0)
    expect(graphData.edgeCount()).toBe(0)
    expect(useGraphStore.getState().findings.size).toBe(1)
  })

  test('clear with a layer removes only that layer ([5-A] clear_layer)', () => {
    apply(
      batch({
        nodes_added: [node('a', { layer: 'l1' }), node('b', { layer: 'l2' })],
      }),
    )

    apply({ op: 'clear', data: { layer: 'l1' }, seq: ++seq })

    expect(graphData.getNode('a')).toBeUndefined()
    expect(graphData.getNode('b')).toBeDefined()
  })

  test('findings survive a layer clear ([23-B] gold is not graph topology)', () => {
    apply(batch({ nodes_added: [node('a', { layer: 'l1' })] }))
    apply({ op: 'finding.add', data: finding('f1', { layer: 'l1' }), seq: ++seq })
    apply({ op: 'finding.add', data: finding('f2', { layer: 'l2' }), seq: ++seq })

    apply({ op: 'clear', data: { layer: 'l1' }, seq: ++seq })

    expect(useGraphStore.getState().findings.has('f1')).toBe(true)
    expect(useGraphStore.getState().findings.has('f2')).toBe(true)
  })

  test('a layer clear drops edges orphaned by the cascade ([4] no dangling edges)', () => {
    // The server cascades cross-layer edges; keeping one here would leave the
    // client holding an edge whose node is gone.
    apply(
      batch({
        nodes_added: [node('a', { layer: 'l1' }), node('b', { layer: 'l2' })],
        edges_added: [edge('a', 'b', { layer: 'l2' })],
      }),
    )

    apply({ op: 'clear', data: { layer: 'l1' }, seq: ++seq })

    expect(graphData.edgeCount()).toBe(0)
    expect(graphData.getNode('b')).toBeDefined()
  })

  test('a layer clear keeps edges whose endpoints both survive', () => {
    apply(
      batch({
        nodes_added: [node('a', { layer: 'l2' }), node('b', { layer: 'l2' })],
        edges_added: [edge('a', 'b', { layer: 'l2' })],
      }),
    )

    apply({ op: 'clear', data: { layer: 'l1' }, seq: ++seq })

    expect(graphData.edgeCount()).toBe(1)
  })
})

describe('local actions ([9-C])', () => {
  test('setFocus', () => {
    useGraphStore.getState().setFocus('a')
    expect(useGraphStore.getState().focus).toBe('a')
  })

  test('setFilter keeps visibleIds until the server answers', () => {
    apply({ op: 'filter.set', data: { expression: 'old', visible_ids: ['a'] }, seq: ++seq })

    useGraphStore.getState().setFilter('new')

    const { filter } = useGraphStore.getState()
    expect(filter.expression).toBe('new')
    expect(filter.visibleIds).toEqual(new Set(['a']))
  })

  test('toggleLayer flips visibility optimistically', () => {
    apply(batch({ nodes_added: [node('a', { layer: 'l1' })] }))

    useGraphStore.getState().toggleLayer('l1')
    expect(useGraphStore.getState().layers.get('l1')?.visible).toBe(false)

    useGraphStore.getState().toggleLayer('l1')
    expect(useGraphStore.getState().layers.get('l1')?.visible).toBe(true)
  })

  test('setViewMode and setZoom', () => {
    useGraphStore.getState().setViewMode('split')
    useGraphStore.getState().setZoom(2.5)

    expect(useGraphStore.getState().viewMode).toBe('split')
    expect(useGraphStore.getState().zoom).toBe(2.5)
  })
})

/**
 * ★ [15] (5) — 타임라인 경계는 push 경로에 O(N+E) 를 두지 않는다 (TASK PERF1).
 *
 * M3c 는 스크러버의 [min,max] 를 structureSeq 마다 전체 스캔으로 구했다. 그것은
 * React render phase 안이라 cosmos effect **앞에** 통째로 얹혔고, push 1회당
 * 노드+엣지+finding 전부를 Date.parse 로 훑었다 — @100K 300,015회.
 *
 * ★ 이 블록이 시간이 아니라 **호출 수**를 세는 이유: 그 회귀는 시간으로 재면
 * 머신 소음(중앙값 111~184ms)에 묻혀 한 렌즈가 "기여 ~0" 으로 오판했지만,
 * 세면 N 에 정확히 비례하는 것이 한 번에 드러난다([15 개정] 측정 규율).
 */
describe('★ [15] (5) 타임라인 경계 — push 경로가 N 에 무관하다', () => {
  function seed(count: number): void {
    const nodes = Array.from({ length: count }, (_, i) =>
      node(`n${i}`, { created_at: `2026-07-17T00:00:${String(i % 60).padStart(2, '0')}+00:00` }),
    )
    const edges = Array.from({ length: count }, (_, i) =>
      edge(`n${i}`, `n${(i + 1) % count}`, {
        created_at: `2026-07-17T00:01:${String(i % 60).padStart(2, '0')}+00:00`,
      }),
    )
    apply(batch({ nodes_added: nodes, edges_added: edges }))
  }

  /** push 1회 + 스크러버가 트랙을 읽는 것까지 — 그 동안의 created_at 파싱 수. */
  function parsesForOnePush(graphSize: number): number {
    useGraphStore.getState().reset()
    seed(graphSize)
    timelineBoundsNow(useGraphStore.getState().findings) // 스크러버가 이미 한 번 읽은 상태
    temporalCounters.parses = 0

    apply(batch({ nodes_added: [node('fresh', { created_at: '2026-07-18T00:00:00+00:00' })] }))
    timelineBoundsNow(useGraphStore.getState().findings)
    return temporalCounters.parses
  }

  test('push 1회당 파싱 수가 그래프 크기와 무관하다', () => {
    const at1k = parsesForOnePush(1_000)
    const at3k = parsesForOnePush(3_000)
    const at10k = parsesForOnePush(10_000)

    // 회귀 판이라면 각각 2,001 / 6,001 / 20,001 로 정확히 선형이었다.
    expect({ at3k, at10k }, '파싱 수가 그래프 크기를 따라간다 — O(N) 이 push 경로에 돌아왔다')
      .toEqual({ at3k: at1k, at10k: at1k })
    // 상수라는 것만으로는 부족하다 — 그 상수가 작아야 한다(추가된 항목 수 수준).
    expect(at1k).toBeLessThan(10)
  })

  test('트랙은 실제로 넓어진다 — 캐시가 낡으면 기능이 죽는다', () => {
    useGraphStore.getState().reset()
    apply(batch({ nodes_added: [node('a', { created_at: '2026-07-17T00:00:00+00:00' })] }))
    const before = timelineBoundsNow(useGraphStore.getState().findings)!

    apply(batch({ nodes_added: [node('b', { created_at: '2026-07-20T00:00:00+00:00' })] }))
    const after = timelineBoundsNow(useGraphStore.getState().findings)!

    expect(after.max).toBeGreaterThan(before.max)
    expect(after.min).toBe(before.min)
  })

  test('finding 과 엣지도 트랙을 넓힌다', () => {
    useGraphStore.getState().reset()
    apply(batch({ nodes_added: [node('a', { created_at: '2026-07-17T12:00:00+00:00' })] }))
    apply({
      op: 'finding.add',
      seq: ++seq,
      data: finding('f1', { created_at: '2026-07-10T00:00:00+00:00' }),
    } as WSEvent)
    apply(batch({ edges_added: [edge('a', 'b', { created_at: '2026-07-25T00:00:00+00:00' })] }))

    const bounds = timelineBoundsNow(useGraphStore.getState().findings)!
    expect(bounds.min).toBe(Date.parse('2026-07-10T00:00:00+00:00')) // finding 이 가장 이르다
    expect(bounds.max).toBe(Date.parse('2026-07-25T00:00:00+00:00')) // 엣지가 가장 늦다
  })

  test('★ 증분 경계가 전체 스캔과 언제나 같다 (삭제·clear 포함)', () => {
    // 증분 유지의 유일한 위험은 "제거 뒤 경계가 낡는 것" 이다. 그래서 무작위
    // 연산 뒤 매번 정답(전체 스캔)과 대조한다 — 캐시가 조용히 어긋나면 여기서
    // 터진다.
    useGraphStore.getState().reset()
    const stamp = (day: number) => `2026-07-${String(day).padStart(2, '0')}T00:00:00+00:00`
    const live: string[] = []

    for (let step = 0; step < 60; step += 1) {
      const day = ((step * 7) % 28) + 1
      const id = `n${step}`
      if (step % 5 === 4 && live.length > 0) {
        const victim = live.splice(step % live.length, 1)[0]
        apply(batch({ nodes_deleted: [{ id: victim }] }))
      } else if (step % 11 === 10) {
        apply({ op: 'clear', seq: ++seq, data: { layer: null } } as WSEvent)
        live.length = 0
      } else {
        apply(batch({ nodes_added: [node(id, { created_at: stamp(day) })] }))
        live.push(id)
      }

      const findings = useGraphStore.getState().findings
      expect(timelineBoundsNow(findings), `step ${step}`).toEqual(
        timelineBounds(graphData.nodes, graphData.edges, findings),
      )
    }
  })
})
