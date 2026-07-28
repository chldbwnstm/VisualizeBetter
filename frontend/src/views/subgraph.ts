/**
 * Focus + N-hop neighbourhood extraction ([7-B], [7-C]).
 *
 * [7-C]: detail 은 focus 노드 + 2-hop 이웃 표시. Pure so the traversal is testable
 * without cytoscape, and so the walk never touches React state ([7-D]).
 */

import type { Edge, Node } from '../types'
import { edgeKeyOf } from '../types'

/** [7-B]: 선택 노드 + N-hop 이웃 (수십~수백). A hub can blow past that. */
export const DEFAULT_DEPTH = 2
export const MAX_SUBGRAPH_NODES = 200

export interface Subgraph {
  nodes: Node[]
  edges: Edge[]
  /** True when the cap cut the neighbourhood short — the UI says so. */
  truncated: boolean
}

export function extractSubgraph(
  allNodes: Map<string, Node>,
  allEdges: Map<string, Edge>,
  focus: string | null,
  depth: number = DEFAULT_DEPTH,
  maxNodes: number = MAX_SUBGRAPH_NODES,
): Subgraph {
  if (!focus || !allNodes.has(focus)) {
    return { nodes: [], edges: [], truncated: false }
  }

  const adjacency = new Map<string, Edge[]>()
  for (const edge of allEdges.values()) {
    if (!adjacency.has(edge.source)) adjacency.set(edge.source, [])
    if (!adjacency.has(edge.target)) adjacency.set(edge.target, [])
    adjacency.get(edge.source)!.push(edge)
    adjacency.get(edge.target)!.push(edge)
  }

  const included = new Set<string>([focus])
  let frontier = [focus]
  let truncated = false

  for (let hop = 0; hop < depth && frontier.length > 0; hop += 1) {
    const next: string[] = []
    for (const id of frontier) {
      for (const edge of adjacency.get(id) ?? []) {
        const other = edge.source === id ? edge.target : edge.source
        if (included.has(other) || !allNodes.has(other)) continue
        if (included.size >= maxNodes) {
          truncated = true
          break
        }
        included.add(other)
        next.push(other)
      }
      if (truncated) break
    }
    frontier = next
  }

  // Only edges with both endpoints inside — a half-drawn edge would imply a
  // neighbour that is not on screen.
  const edges: Edge[] = []
  const seen = new Set<string>()
  for (const edge of allEdges.values()) {
    if (!included.has(edge.source) || !included.has(edge.target)) continue
    const key = edgeKeyOf(edge)
    if (seen.has(key)) continue
    seen.add(key)
    edges.push(edge)
  }

  return {
    nodes: [...included].map((id) => allNodes.get(id)!),
    edges,
    truncated,
  }
}
