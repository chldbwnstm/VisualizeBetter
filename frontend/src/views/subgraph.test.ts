/**
 * Completion verification for TASK 7c — subgraph extraction ([7-B], [7-C]).
 */

import { expect, test } from 'vitest'
import { DEFAULT_DEPTH, extractSubgraph } from './subgraph'
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

function edge(source: string, target: string, key = ''): Edge {
  return {
    source,
    target,
    relation: 'field',
    key,
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

/** a — b — c — d chain */
function chain() {
  const nodes = new Map(['a', 'b', 'c', 'd'].map((id) => [id, node(id)]))
  const edges = new Map(
    [edge('a', 'b'), edge('b', 'c'), edge('c', 'd')].map((e) => [
      `${e.source}|${e.target}|${e.relation}|${e.key}`,
      e,
    ]),
  )
  return { nodes, edges }
}

test('default depth is 2 ([7-C]: focus + 2-hop)', () => {
  expect(DEFAULT_DEPTH).toBe(2)
})

test('reaches exactly 2 hops from focus', () => {
  const { nodes, edges } = chain()

  const result = extractSubgraph(nodes, edges, 'a')

  expect(new Set(result.nodes.map((n) => n.id))).toEqual(new Set(['a', 'b', 'c']))
  expect(result.nodes.map((n) => n.id)).not.toContain('d')
})

test('depth is configurable', () => {
  const { nodes, edges } = chain()

  const result = extractSubgraph(nodes, edges, 'a', 3)

  expect(result.nodes).toHaveLength(4)
})

test('follows edges in both directions', () => {
  const { nodes, edges } = chain()

  const result = extractSubgraph(nodes, edges, 'd', 1)

  expect(new Set(result.nodes.map((n) => n.id))).toEqual(new Set(['d', 'c']))
})

test('only includes edges with both endpoints on screen', () => {
  const { nodes, edges } = chain()

  const result = extractSubgraph(nodes, edges, 'a')

  // c—d is excluded: d is not in the subgraph, so drawing it would imply an
  // off-screen neighbour.
  expect(result.edges.map((e) => `${e.source}->${e.target}`)).toEqual(['a->b', 'b->c'])
})

test('a focus with no neighbours is just itself', () => {
  const nodes = new Map([['lonely', node('lonely')]])

  const result = extractSubgraph(nodes, new Map(), 'lonely')

  expect(result.nodes.map((n) => n.id)).toEqual(['lonely'])
  expect(result.edges).toEqual([])
})

test('no focus yields nothing', () => {
  const { nodes, edges } = chain()

  expect(extractSubgraph(nodes, edges, null)).toEqual({ nodes: [], edges: [], truncated: false })
})

test('an unknown focus yields nothing', () => {
  const { nodes, edges } = chain()

  expect(extractSubgraph(nodes, edges, 'ghost').nodes).toEqual([])
})

test('parallel edges are all kept', () => {
  const nodes = new Map([node('a'), node('b')].map((n) => [n.id, n]))
  const edges = new Map(
    [edge('a', 'b', 'k1'), edge('a', 'b', 'k2')].map((e) => [
      `${e.source}|${e.target}|${e.relation}|${e.key}`,
      e,
    ]),
  )

  expect(extractSubgraph(nodes, edges, 'a').edges).toHaveLength(2)
})

test('a huge hub is capped and says so ([7-B]: 수십~수백)', () => {
  const nodes = new Map([['hub', node('hub')]])
  const edges = new Map<string, Edge>()
  for (let i = 0; i < 500; i += 1) {
    const id = `n${i}`
    nodes.set(id, node(id))
    edges.set(`hub|${id}|field|`, edge('hub', id))
  }

  const result = extractSubgraph(nodes, edges, 'hub', 2, 50)

  expect(result.truncated).toBe(true)
  expect(result.nodes.length).toBeLessThanOrEqual(50)
})
