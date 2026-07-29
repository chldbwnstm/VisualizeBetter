/**
 * [13-B] CH2(7a) — Detail element ids must be injective.
 *
 * The id was `${source}|${target}|${relation}|${key}`, which collides whenever a
 * component contains '|': relation="x", key="y|z" and relation="x|y", key="z"
 * both render as "a|b|x|y|z". cytoscape keeps the first element with that id and
 * drops the second — no exception, no console error (verified against the real
 * headless cytoscape). A relation just disappears from the detail view.
 *
 * The store has always used JSON.stringify for the same tuple, which is
 * injective; only this view diverged.
 */
import cytoscape from 'cytoscape'
import { describe, expect, test } from 'vitest'

import { toCytoscapeElements } from './DetailPanel'
import type { Subgraph } from './subgraph'

const NODES = [
  { id: 'a', label: 'A', type: 'class' },
  { id: 'b', label: 'B', type: 'class' },
]

const COLLIDING = [
  { source: 'a', target: 'b', relation: 'x', key: 'y|z', directed: true },
  { source: 'a', target: 'b', relation: 'x|y', key: 'z', directed: true },
]

function subgraph(): Subgraph {
  return { nodes: NODES, edges: COLLIDING } as unknown as Subgraph
}

describe('★ Detail element ids are injective', () => {
  test('구분자가 든 두 엣지가 서로 다른 id 를 받는다', () => {
    const elements = toCytoscapeElements(subgraph(), 'a', [], null)
    const edgeIds = elements
      .filter((el) => el.data.source !== undefined)
      .map((el) => el.data.id)
    expect(new Set(edgeIds).size).toBe(2)
  })

  test('cytoscape 가 두 엣지를 모두 유지한다 (이전엔 조용히 1개로 접혔다)', () => {
    const elements = toCytoscapeElements(subgraph(), 'a', [], null)
    const cy = cytoscape({ headless: true, elements: elements as cytoscape.ElementDefinition[] })
    expect(cy.edges().length).toBe(2)
  })
})
