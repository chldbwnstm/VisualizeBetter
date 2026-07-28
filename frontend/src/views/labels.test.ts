/**
 * Completion verification for TASK 7d — label selection + LOD ([7-A]).
 */

import { describe, expect, test } from 'vitest'
import {
  LABEL_ZOOM_THRESHOLD,
  MAX_LABELS,
  pickTrackedIndices,
  selectLabels,
} from './labels'
import { buildGraphArrays } from './graphAdapter'
import type { Edge, Node } from '../types'

function node(id: string): Node {
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
    created_at: 't',
    updated_at: 't',
    created_by: null,
  }
}

function edge(source: string, target: string): Edge {
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
    created_at: 't',
    created_by: null,
  }
}

/** hub has 3 edges; a/b/c have 1 each; lonely has none. */
function arrays() {
  const nodes = new Map(['hub', 'a', 'b', 'c', 'lonely'].map((id) => [id, node(id)]))
  const edges = new Map(
    [edge('hub', 'a'), edge('hub', 'b'), edge('hub', 'c')].map((e) => [
      `${e.source}|${e.target}|${e.relation}|${e.key}`,
      e,
    ]),
  )
  return { built: buildGraphArrays(nodes, edges), nodes }
}

function source(over: Partial<Parameters<typeof selectLabels>[0]> = {}) {
  const { built, nodes } = arrays()
  const tracked = new Map<number, [number, number]>()
  built.ids.forEach((_, index) => tracked.set(index, [index * 10, index * 10]))
  return {
    tracked,
    arrays: built,
    zoom: 1,
    labelOf: (id: string) => nodes.get(id)?.label,
    toScreen: (p: [number, number]) => [p[0] + 1, p[1] + 2] as [number, number],
    ...over,
  }
}

describe('pickTrackedIndices ([7-A] 상위 N)', () => {
  test('tracks the highest-degree points first', () => {
    const { built } = arrays()

    const picked = pickTrackedIndices(built, 1)

    expect(picked).toEqual([built.indexById.get('hub')])
  })

  test('caps at maxLabels', () => {
    const { built } = arrays()

    expect(pickTrackedIndices(built, 3)).toHaveLength(3)
  })

  test('is deterministic', () => {
    const { built } = arrays()

    expect(pickTrackedIndices(built)).toEqual(pickTrackedIndices(built))
  })

  test('zero cap tracks nothing', () => {
    const { built } = arrays()

    expect(pickTrackedIndices(built, 0)).toEqual([])
  })

  test('default cap is MAX_LABELS', () => {
    expect(MAX_LABELS).toBe(30)
  })
})

describe('[7-A] LOD', () => {
  test('zoom 낮음 → 라벨 없음 (점만)', () => {
    expect(selectLabels(source({ zoom: LABEL_ZOOM_THRESHOLD - 0.01 }))).toEqual([])
  })

  test('zoom 높음 → 라벨 표시', () => {
    expect(selectLabels(source({ zoom: LABEL_ZOOM_THRESHOLD })).length).toBeGreaterThan(0)
  })

  test('the threshold clears a fitted small graph', () => {
    // cosmos fits the view on init; a handful of nodes lands near ~0.86, and a
    // freshly pushed graph must be readable without the user zooming first.
    expect(selectLabels(source({ zoom: 0.86 })).length).toBeGreaterThan(0)
  })

  test('상위 N 개만', () => {
    expect(selectLabels(source({ maxLabels: 2 }))).toHaveLength(2)
  })
})

describe('ranking and placement', () => {
  test('hub 이 먼저 이름을 얻는다', () => {
    expect(selectLabels(source({ maxLabels: 1 }))[0].id).toBe('hub')
  })

  test('space 좌표를 screen 으로 변환해 배치', () => {
    const base = source({ maxLabels: 1 })
    const hubIndex = base.arrays.indexById.get('hub')!

    const [placement] = selectLabels(base)

    expect(placement.x).toBe(hubIndex * 10 + 1)
    expect(placement.y).toBe(hubIndex * 10 + 2)
  })

  test('라벨 텍스트는 노드 label 이다', () => {
    expect(selectLabels(source({ maxLabels: 1 }))[0].label).toBe('HUB')
  })

  test('결정적 순서', () => {
    expect(selectLabels(source()).map((p) => p.id)).toEqual(
      selectLabels(source()).map((p) => p.id),
    )
  })
})

describe('[7-A] 화면 내 only', () => {
  test('off-screen labels are dropped', () => {
    const base = source({ viewport: { width: 100, height: 100 } })

    const placements = selectLabels({
      ...base,
      toScreen: () => [5000, 5000],
    })

    expect(placements).toEqual([])
  })

  test('on-screen labels are kept', () => {
    const base = source({ viewport: { width: 100, height: 100 } })

    const placements = selectLabels({ ...base, toScreen: () => [50, 50] })

    expect(placements.length).toBeGreaterThan(0)
  })

  test('without a viewport nothing is culled', () => {
    const placements = selectLabels({ ...source(), toScreen: () => [5000, 5000] })

    expect(placements.length).toBeGreaterThan(0)
  })
})

describe('robustness', () => {
  test('a stale tracked index from before the last rebuild is ignored', () => {
    const base = source()
    const stale = new Map(base.tracked)
    stale.set(9999, [0, 0])

    expect(() => selectLabels({ ...base, tracked: stale })).not.toThrow()
    expect(selectLabels({ ...base, tracked: stale }).every((p) => p.id)).toBe(true)
  })

  test('a removed point reads back NaN and gets no label', () => {
    const base = source()
    const withNaN = new Map<number, [number, number]>([[0, [NaN, NaN]]])

    expect(selectLabels({ ...base, tracked: withNaN })).toEqual([])
  })

  test('a node without a label is skipped', () => {
    expect(selectLabels({ ...source(), labelOf: () => undefined })).toEqual([])
  })

  test('nothing tracked → no labels', () => {
    expect(selectLabels(source({ tracked: new Map() }))).toEqual([])
  })

  test('maxLabels 0 → no labels', () => {
    expect(selectLabels(source({ maxLabels: 0 }))).toEqual([])
  })
})
