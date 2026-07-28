/**
 * Completion verification for TASK 7c — cosmos data adapter ([7-A], [7-D]).
 *
 * The adapter is where the real logic lives (degree, colour, index mapping), so
 * it is tested directly — no WebGL context involved.
 */

import { describe, expect, test } from 'vitest'
import {
  MAX_POINT_SIZE,
  MIN_POINT_SIZE,
  buildGraphArrays,
  colorForNode,
  degreeOf,
  idAtIndex,
  sizeForDegree,
} from './graphAdapter'
import { PLACEHOLDER_RGB } from './palette'
import type { Edge, Node } from '../types'

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
    created_at: 't',
    updated_at: 't',
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
    created_at: 't',
    created_by: null,
    ...over,
  }
}

function maps(nodes: Node[], edges: Edge[] = []) {
  return {
    nodes: new Map(nodes.map((n) => [n.id, n])),
    edges: new Map(edges.map((e) => [`${e.source}|${e.target}|${e.relation}|${e.key}`, e])),
  }
}

describe('degree ([7-A] 크기 = degree)', () => {
  test('counts both endpoints', () => {
    const degree = degreeOf([edge('a', 'b'), edge('a', 'c')])

    expect(degree.get('a')).toBe(2)
    expect(degree.get('b')).toBe(1)
  })

  test('a self loop counts twice', () => {
    expect(degreeOf([edge('a', 'a')]).get('a')).toBe(2)
  })
})

describe('sizeForDegree — log scale', () => {
  test('an isolated node is the minimum', () => {
    expect(sizeForDegree(0, 10)).toBe(MIN_POINT_SIZE)
  })

  test('the biggest hub is the maximum', () => {
    expect(sizeForDegree(10, 10)).toBeCloseTo(MAX_POINT_SIZE)
  })

  test('log scale keeps a mega-hub from swamping the rest', () => {
    // Linear scaling would put degree 10 at 1% of the way to max.
    const mid = sizeForDegree(10, 1000)
    const fraction = (mid - MIN_POINT_SIZE) / (MAX_POINT_SIZE - MIN_POINT_SIZE)
    expect(fraction).toBeGreaterThan(0.3)
  })

  test('no edges at all is safe', () => {
    expect(sizeForDegree(0, 0)).toBe(MIN_POINT_SIZE)
  })
})

describe('colorForNode ([7-A] 팔레트)', () => {
  test('the same type always gets the same colour', () => {
    expect(colorForNode(node('a', { type: 'class' }), 'type')).toEqual(
      colorForNode(node('b', { type: 'class' }), 'type'),
    )
  })

  test('a placeholder is dimmed, not given a type colour ([5-A])', () => {
    const placeholder = node('ghost', { type: 'unresolved', properties: { placeholder: true } })

    expect(colorForNode(placeholder, 'type')).toEqual(PLACEHOLDER_RGB)
  })

  test('colouring by layer uses the layer', () => {
    const a = colorForNode(node('a', { type: 'class', layer: 'l1' }), 'layer')
    const b = colorForNode(node('b', { type: 'function', layer: 'l1' }), 'layer')

    expect(a).toEqual(b)
  })
})

describe('buildGraphArrays ([7-D] typed arrays)', () => {
  test('produces one position pair and one colour quad per node', () => {
    const { nodes, edges } = maps([node('a'), node('b')])

    const arrays = buildGraphArrays(nodes, edges)

    expect(arrays.pointPositions).toHaveLength(4)
    expect(arrays.pointColors).toHaveLength(8)
    expect(arrays.pointSizes).toHaveLength(2)
  })

  test('links reference point indices, not ids', () => {
    const { nodes, edges } = maps([node('a'), node('b')], [edge('a', 'b')])

    const arrays = buildGraphArrays(nodes, edges)

    expect([...arrays.links]).toEqual([arrays.indexById.get('a'), arrays.indexById.get('b')])
  })

  test('index maps back to id for the click handler', () => {
    const { nodes, edges } = maps([node('a'), node('b')])

    const arrays = buildGraphArrays(nodes, edges)

    expect(idAtIndex(arrays, arrays.indexById.get('b')!)).toBe('b')
  })

  test('an edge to a node we do not hold is dropped, not drawn to -1', () => {
    const { nodes, edges } = maps([node('a')], [edge('a', 'missing')])

    const arrays = buildGraphArrays(nodes, edges)

    expect(arrays.links).toHaveLength(0)
  })

  test('colours are normalized to 0..1 for the GPU', () => {
    const { nodes, edges } = maps([node('a')])

    const arrays = buildGraphArrays(nodes, edges)

    for (const channel of arrays.pointColors) {
      expect(channel).toBeGreaterThanOrEqual(0)
      expect(channel).toBeLessThanOrEqual(1)
    }
  })

  test('positions are deterministic for the same id', () => {
    const first = buildGraphArrays(...Object.values(maps([node('a')])) as [never, never])
    const second = buildGraphArrays(...Object.values(maps([node('a')])) as [never, never])

    expect([...first.pointPositions]).toEqual([...second.pointPositions])
  })

  test('an empty graph produces empty arrays', () => {
    const arrays = buildGraphArrays(new Map(), new Map())

    expect(arrays.pointPositions).toHaveLength(0)
    expect(arrays.links).toHaveLength(0)
  })

  test('a hub is drawn bigger than a leaf', () => {
    const { nodes, edges } = maps(
      [node('hub'), node('a'), node('b'), node('c')],
      [edge('hub', 'a'), edge('hub', 'b'), edge('hub', 'c')],
    )

    const arrays = buildGraphArrays(nodes, edges)
    const hub = arrays.pointSizes[arrays.indexById.get('hub')!]
    const leaf = arrays.pointSizes[arrays.indexById.get('a')!]

    expect(hub).toBeGreaterThan(leaf)
  })
})

// --- M3b: IncrementalCosmosArrays (O(delta) maintenance == buildGraphArrays) ---

import { IncrementalCosmosArrays } from './graphAdapter'
import { edgeKeyOf } from '../types'

function edgeMap(es: Edge[]) {
  return new Map(es.map((e) => [edgeKeyOf(e), e]))
}

describe('IncrementalCosmosArrays ([7-D] incremental, no full reset)', () => {
  test('★ a single applyAdd from empty is byte-identical to buildGraphArrays', () => {
    const ns = [node('a'), node('b'), node('hub'), node('c')]
    const es = [edge('hub', 'a'), edge('hub', 'b'), edge('hub', 'c'), edge('a', 'b')]
    const nodes = new Map(ns.map((n) => [n.id, n]))
    const edges = edgeMap(es)
    const full = buildGraphArrays(nodes, edges)

    const inc = new IncrementalCosmosArrays()
    const arr = inc.applyAdd(
      { addedNodes: [...nodes.keys()], addedEdges: [...edges.keys()], touchedNodes: [] },
      nodes,
      edges,
    )
    expect(arr.ids).toEqual(full.ids)
    expect([...arr.pointPositions]).toEqual([...full.pointPositions])
    expect([...arr.pointColors]).toEqual([...full.pointColors])
    expect([...arr.pointSizes]).toEqual([...full.pointSizes])
    expect([...arr.links]).toEqual([...full.links])
  })

  test('★ adding an edge resizes both endpoints via degree ([7-A] degree sizing)', () => {
    const nodes = new Map([node('a'), node('b'), node('c')].map((n) => [n.id, n]))
    const inc = new IncrementalCosmosArrays()
    let arr = inc.applyAdd({ addedNodes: ['a', 'b', 'c'], addedEdges: [], touchedNodes: [] }, nodes, new Map())
    expect(arr.pointSizes[arr.indexById.get('a')!]).toBe(MIN_POINT_SIZE) // isolated

    const e = edge('a', 'b')
    const edges = edgeMap([e])
    arr = inc.applyAdd({ addedNodes: [], addedEdges: [edgeKeyOf(e)], touchedNodes: [] }, nodes, edges)
    // a,b now degree 1 (and maxDegree grew 0→1, so they read as hubs); c stays min.
    expect(arr.pointSizes[arr.indexById.get('a')!]).toBeGreaterThan(MIN_POINT_SIZE)
    expect(arr.pointSizes[arr.indexById.get('b')!]).toBeGreaterThan(MIN_POINT_SIZE)
    expect(arr.pointSizes[arr.indexById.get('c')!]).toBe(MIN_POINT_SIZE)
    // and identical to a full rebuild of the same final state.
    expect([...arr.pointSizes]).toEqual([...buildGraphArrays(nodes, edges).pointSizes])
  })

  test('★ an edge that does not raise maxDegree touches only its endpoints (O(delta))', () => {
    const nodes = new Map([node('hub'), node('a'), node('b'), node('c')].map((n) => [n.id, n]))
    const base = [edge('hub', 'a'), edge('hub', 'b'), edge('hub', 'c')]
    const inc = new IncrementalCosmosArrays()
    const s0 = inc.rebuild(nodes, edgeMap(base)) // hub degree 3 = maxDegree
    const hubBefore = s0.pointSizes[s0.indexById.get('hub')!]
    const cBefore = s0.pointSizes[s0.indexById.get('c')!]

    const e = edge('a', 'b') // a,b → degree 2; maxDegree stays 3
    const edges = edgeMap([...base, e])
    const arr = inc.applyAdd({ addedNodes: [], addedEdges: [edgeKeyOf(e)], touchedNodes: [] }, nodes, edges)
    // ★ non-endpoints unchanged — the resize loop ran over the delta, not all N.
    expect(arr.pointSizes[arr.indexById.get('hub')!]).toBe(hubBefore)
    expect(arr.pointSizes[arr.indexById.get('c')!]).toBe(cBefore)
    expect(arr.pointSizes[arr.indexById.get('a')!]).toBeGreaterThan(MIN_POINT_SIZE)
    expect([...arr.pointSizes]).toEqual([...buildGraphArrays(nodes, edges).pointSizes])
  })

  test('a pure node-add appends without disturbing existing points', () => {
    const nodes = new Map([node('a'), node('b')].map((n) => [n.id, n]))
    const inc = new IncrementalCosmosArrays()
    const before = inc.applyAdd({ addedNodes: ['a', 'b'], addedEdges: [], touchedNodes: [] }, nodes, new Map())
    const posA = [before.pointPositions[0], before.pointPositions[1]]
    const colA = [...before.pointColors.slice(0, 4)]

    nodes.set('c', node('c'))
    const after = inc.applyAdd({ addedNodes: ['c'], addedEdges: [], touchedNodes: [] }, nodes, new Map())
    expect(after.ids).toEqual(['a', 'b', 'c'])
    // a's slot is untouched (append, not rebuild).
    expect([after.pointPositions[0], after.pointPositions[1]]).toEqual(posA)
    expect([...after.pointColors.slice(0, 4)]).toEqual(colA)
    expect([...after.pointColors]).toEqual([...buildGraphArrays(nodes, new Map()).pointColors])
  })

  test('touch recolours in place and keeps the position (overlay path)', () => {
    const nodes = new Map([node('a', { type: 'class' })].map((n) => [n.id, n]))
    const inc = new IncrementalCosmosArrays()
    const before = inc.applyAdd({ addedNodes: ['a'], addedEdges: [], touchedNodes: [] }, nodes, new Map())
    const pos = [before.pointPositions[0], before.pointPositions[1]]
    const col = [...before.pointColors]

    nodes.set('a', node('a', { type: 'service' }))
    const after = inc.touch(['a'], nodes)
    expect([after.pointPositions[0], after.pointPositions[1]]).toEqual(pos) // unmoved
    expect([...after.pointColors]).not.toEqual(col) // recoloured
  })

  test('rebuild matches buildGraphArrays and a later add continues incrementally', () => {
    const nodes = new Map([node('a'), node('b')].map((n) => [n.id, n]))
    const edges = edgeMap([edge('a', 'b')])
    const inc = new IncrementalCosmosArrays()
    const rebuilt = inc.rebuild(nodes, edges)
    expect([...rebuilt.pointSizes]).toEqual([...buildGraphArrays(nodes, edges).pointSizes])

    nodes.set('c', node('c'))
    const after = inc.applyAdd({ addedNodes: ['c'], addedEdges: [], touchedNodes: [] }, nodes, edges)
    expect(after.ids).toEqual(['a', 'b', 'c'])
    expect([...after.pointColors]).toEqual([...buildGraphArrays(nodes, edges).pointColors])
  })

  test('a batch that adds nodes and their edges clusters like the full build', () => {
    // hub + leaves arriving together, edges in the same batch.
    const ns = [node('hub'), node('l1'), node('l2')]
    const es = [edge('hub', 'l1'), edge('hub', 'l2')]
    const nodes = new Map(ns.map((n) => [n.id, n]))
    const edges = edgeMap(es)
    const inc = new IncrementalCosmosArrays()
    const arr = inc.applyAdd(
      { addedNodes: [...nodes.keys()], addedEdges: [...edges.keys()], touchedNodes: [] },
      nodes,
      edges,
    )
    expect([...arr.pointPositions]).toEqual([...buildGraphArrays(nodes, edges).pointPositions])
    expect([...arr.links]).toEqual([...buildGraphArrays(nodes, edges).links])
  })
})
