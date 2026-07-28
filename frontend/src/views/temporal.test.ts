/**
 * Completion verification for TASK M3c — temporal scrubber (created_at overlay).
 *
 * The overlay's logic — which ids are visible at a cutoff, which links survive, the
 * timeline bounds — is pure and tested here without a WebGL context.
 */

import { describe, expect, test } from 'vitest'
import { timelineBounds, toMs, visibleFindingIds, visibleLinks, visibleNodeIds } from './temporal'
import { edgeKeyOf } from '../types'
import type { Edge, Finding, Node } from '../types'

function node(id: string, createdAt: string): Node {
  return {
    id, label: id, type: 'class', properties: {}, parent_id: null, style_hint: null,
    position_hint: null, layer: null, ttl: 0, tags: [], created_at: createdAt,
    updated_at: createdAt, created_by: null,
  }
}
function edge(source: string, target: string, createdAt: string): Edge {
  return {
    source, target, relation: 'r', key: '', directed: true, properties: {}, weight: 1,
    layer: null, style_hint: null, ttl: 0, tags: [], created_at: createdAt, created_by: null,
  }
}
function finding(id: string, createdAt: string): Finding {
  return {
    finding_id: id, title: id, body: '', node_ids: [], confidence: 0.8, evidence: [],
    layer: null, tags: [], created_by: null, created_at: createdAt, updated_at: createdAt,
  } as Finding
}
const T = (s: string) => toMs(`2026-01-01T00:00:0${s}Z`)
function nodeMap(ns: Node[]) { return new Map(ns.map((n) => [n.id, n])) }
function edgeMap(es: Edge[]) { return new Map(es.map((e) => [edgeKeyOf(e), e])) }
function findingMap(fs: Finding[]) { return new Map(fs.map((f) => [f.finding_id, f])) }

describe('temporal — timelineBounds', () => {
  test('spans the earliest and latest created_at across nodes/edges/findings', () => {
    const b = timelineBounds(
      nodeMap([node('a', '2026-01-01T00:00:01Z'), node('b', '2026-01-01T00:00:05Z')]),
      edgeMap([edge('a', 'b', '2026-01-01T00:00:07Z')]),
      findingMap([finding('f', '2026-01-01T00:00:03Z')]),
    )
    expect(b).toEqual({ min: T('1'), max: T('7') })
  })

  test('an empty graph has no timeline', () => {
    expect(timelineBounds(new Map(), new Map(), new Map())).toBeNull()
  })
})

describe('temporal — visibleNodeIds (created_at <= cutoff)', () => {
  const nodes = nodeMap([
    node('a', '2026-01-01T00:00:01Z'),
    node('b', '2026-01-01T00:00:05Z'),
    node('c', '2026-01-01T00:00:09Z'),
  ])

  test('a null cutoff means live — no overlay', () => {
    expect(visibleNodeIds(nodes, null)).toBeNull()
  })

  test('shows only nodes at or before the cutoff', () => {
    expect(visibleNodeIds(nodes, T('5'))).toEqual(new Set(['a', 'b']))
  })

  test('the min cutoff shows only the first node; the max shows all', () => {
    expect(visibleNodeIds(nodes, T('1'))).toEqual(new Set(['a']))
    expect(visibleNodeIds(nodes, T('9'))).toEqual(new Set(['a', 'b', 'c']))
  })

  test('★ advancing the cutoff only ever reveals more (monotonic growth)', () => {
    const at1 = visibleNodeIds(nodes, T('1'))!
    const at5 = visibleNodeIds(nodes, T('5'))!
    const at9 = visibleNodeIds(nodes, T('9'))!
    expect([...at1].every((id) => at5.has(id))).toBe(true)
    expect([...at5].every((id) => at9.has(id))).toBe(true)
  })
})

describe('temporal — visibleFindingIds', () => {
  test('gates findings by created_at like nodes', () => {
    const fs = findingMap([finding('f1', '2026-01-01T00:00:02Z'), finding('f2', '2026-01-01T00:00:08Z')])
    expect(visibleFindingIds(fs, T('5'))).toEqual(new Set(['f1']))
    expect(visibleFindingIds(fs, null)).toBeNull()
  })
})

describe('temporal — visibleLinks (edge + endpoint consistency)', () => {
  const nodes = nodeMap([
    node('a', '2026-01-01T00:00:01Z'),
    node('b', '2026-01-01T00:00:02Z'),
    node('c', '2026-01-01T00:00:08Z'),
  ])
  const indexById = new Map([['a', 0], ['b', 1], ['c', 2]])
  const edges = edgeMap([
    edge('a', 'b', '2026-01-01T00:00:03Z'), // present at T5
    edge('b', 'c', '2026-01-01T00:00:09Z'), // c and this edge are future at T5
  ])

  test('only edges at or before the cutoff are drawn, as index pairs', () => {
    const visible = visibleNodeIds(nodes, T('5'))!
    expect([...visibleLinks(edges, indexById, visible, T('5'))]).toEqual([0, 1]) // a→b only
  })

  test('★ a visible edge never dangles: both endpoints are visible', () => {
    const visible = visibleNodeIds(nodes, T('5'))!
    const links = visibleLinks(edges, indexById, visible, T('5'))
    for (let i = 0; i < links.length; i += 2) {
      // every referenced index maps back to a visible node
      const src = [...indexById].find(([, idx]) => idx === links[i])![0]
      const tgt = [...indexById].find(([, idx]) => idx === links[i + 1])![0]
      expect(visible.has(src) && visible.has(tgt)).toBe(true)
    }
  })

  test('at the max cutoff every edge is drawn', () => {
    const visible = visibleNodeIds(nodes, T('9'))!
    expect([...visibleLinks(edges, indexById, visible, T('9'))]).toEqual([0, 1, 1, 2])
  })
})
