/**
 * Completion verification for TASK 7c — anchor highlight ([23-F] TASK 7, [23-B]).
 *
 * The highlight must never change the graph: [23-B] is explicit that a finding is
 * a weak link rendered as an overlay, and that graph topology stays untouched.
 */

import { describe, expect, test } from 'vitest'
import { applyHighlight, parseColor } from './OverviewCanvas'
import { buildGraphArrays } from './graphAdapter'
import type { Node } from '../types'

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

function arraysFor(ids: string[]) {
  return buildGraphArrays(new Map(ids.map((id) => [id, node(id)])), new Map())
}

describe('applyHighlight', () => {
  test('recolours the anchor', () => {
    const arrays = arraysFor(['a', 'b'])
    const index = arrays.indexById.get('a')!

    const { colors } = applyHighlight(arrays, ['a'])

    // Float32Array rounds, so compare per channel rather than exactly.
    const rgba = [...colors.slice(index * 4, index * 4 + 4)]
    expect(rgba[0]).toBeCloseTo(1)
    expect(rgba[1]).toBeCloseTo(0.85)
    expect(rgba[2]).toBeCloseTo(0.2)
    expect(rgba[3]).toBeCloseTo(1)
  })

  test('enlarges the anchor', () => {
    const arrays = arraysFor(['a'])
    const index = arrays.indexById.get('a')!

    const { sizes } = applyHighlight(arrays, ['a'])

    expect(sizes[index]).toBeGreaterThan(arrays.pointSizes[index])
  })

  test('leaves non-anchors alone', () => {
    const arrays = arraysFor(['a', 'b'])
    const other = arrays.indexById.get('b')!

    const { colors, sizes } = applyHighlight(arrays, ['a'])

    expect([...colors.slice(other * 4, other * 4 + 4)]).toEqual(
      [...arrays.pointColors.slice(other * 4, other * 4 + 4)],
    )
    expect(sizes[other]).toBe(arrays.pointSizes[other])
  })

  test('★ graph topology is untouched ([23-B])', () => {
    const arrays = arraysFor(['a', 'b'])
    const positionsBefore = [...arrays.pointPositions]
    const linksBefore = [...arrays.links]

    applyHighlight(arrays, ['a'])

    expect([...arrays.pointPositions]).toEqual(positionsBefore)
    expect([...arrays.links]).toEqual(linksBefore)
    // The message must be expect()'s 2nd arg — `expect(x).toHaveLength(2), 'msg'`
    // is a comma operator that evaluates and discards the string (audit #18).
    expect(arrays.ids, 'no node was added for the finding').toHaveLength(2)
  })

  test('does not mutate the source arrays — the overlay is a copy', () => {
    const arrays = arraysFor(['a'])
    const original = [...arrays.pointColors]

    applyHighlight(arrays, ['a'])

    expect([...arrays.pointColors]).toEqual(original)
  })

  test('highlights every anchor of the finding, not just the first', () => {
    const arrays = arraysFor(['a', 'b', 'c'])

    const { sizes } = applyHighlight(arrays, ['a', 'c'])

    expect(sizes[arrays.indexById.get('a')!]).toBeGreaterThan(arrays.pointSizes[0])
    expect(sizes[arrays.indexById.get('c')!]).toBeGreaterThan(arrays.pointSizes[0])
  })

  test('an anchor that has not been pushed yet is skipped, not a crash', () => {
    // [23-B]: finding.node_ids may point at a node that does not exist yet.
    const arrays = arraysFor(['a'])

    expect(() => applyHighlight(arrays, ['not-pushed-yet'])).not.toThrow()
  })

  test('no anchors is a no-op', () => {
    const arrays = arraysFor(['a'])

    const { colors } = applyHighlight(arrays, [])

    expect([...colors]).toEqual([...arrays.pointColors])
  })
})

describe('applyHighlight filter dim ([5-C])', () => {
  const visible = (...ids: string[]) => ({ visibleIds: new Set(ids) })

  test('dims a node the filter does not match', () => {
    const arrays = arraysFor(['a', 'b'])
    const b = arrays.indexById.get('b')!

    const { colors } = applyHighlight(arrays, [], visible('a'))

    // b is not in the visible set → greyed, low alpha.
    expect(colors[b * 4 + 3]).toBeLessThan(0.5)
    expect([...colors.slice(b * 4, b * 4 + 3)]).not.toEqual(
      [...arrays.pointColors.slice(b * 4, b * 4 + 3)],
    )
  })

  test('leaves a matching node at its normal colour', () => {
    const arrays = arraysFor(['a', 'b'])
    const a = arrays.indexById.get('a')!

    const { colors } = applyHighlight(arrays, [], visible('a'))

    expect([...colors.slice(a * 4, a * 4 + 4)]).toEqual(
      [...arrays.pointColors.slice(a * 4, a * 4 + 4)],
    )
  })

  test('★ an anchor wins over dim — a selected anchor stays gold even if filtered out', () => {
    const arrays = arraysFor(['a', 'b'])
    const a = arrays.indexById.get('a')!

    // 'a' is not in the visible set (would dim) but is a selected anchor.
    const { colors } = applyHighlight(arrays, ['a'], visible('b'))

    const rgba = [...colors.slice(a * 4, a * 4 + 4)]
    expect(rgba[0]).toBeCloseTo(1) // amber, not grey
    expect(rgba[3]).toBeCloseTo(1)
  })

  test('no dim argument leaves every node normal', () => {
    const arrays = arraysFor(['a', 'b'])

    const { colors } = applyHighlight(arrays, [])

    expect([...colors]).toEqual([...arrays.pointColors])
  })

  test('dim does not add or remove nodes ([23-B] topology unchanged)', () => {
    const arrays = arraysFor(['a', 'b'])
    const links = [...arrays.links]

    applyHighlight(arrays, [], visible('a'))

    expect(arrays.ids).toHaveLength(2)
    expect([...arrays.links]).toEqual(links)
  })
})

describe('parseColor', () => {
  test('parses #rrggbb', () => {
    expect(parseColor('#ff8000')).toEqual([1, 128 / 255, 0, 1])
  })
  test('parses #rgb shorthand', () => {
    expect(parseColor('#f80')).toEqual([1, 136 / 255, 0, 1])
  })
  test('parses rgba', () => {
    expect(parseColor('rgba(255,0,0,0.5)')).toEqual([1, 0, 0, 0.5])
  })
  test('returns null for something unparseable', () => {
    expect(parseColor('nonsense')).toBeNull()
  })
})

describe('applyHighlight AI style ([5-D])', () => {
  const style = (ids: string[], s: { color?: string; size?: number }) => [{ ids, style: s }]

  test('recolours a styled node', () => {
    const arrays = arraysFor(['a', 'b'])
    const a = arrays.indexById.get('a')!

    const { colors } = applyHighlight(arrays, [], null, style(['a'], { color: '#00ff00' }))

    expect([...colors.slice(a * 4, a * 4 + 4)]).toEqual([0, 1, 0, 1])
  })

  test('resizes a styled node', () => {
    const arrays = arraysFor(['a'])
    const a = arrays.indexById.get('a')!

    const { sizes } = applyHighlight(arrays, [], null, style(['a'], { size: 42 }))

    expect(sizes[a]).toBe(42)
  })

  test('a later style wins on the same node (stack)', () => {
    const arrays = arraysFor(['a'])
    const a = arrays.indexById.get('a')!

    const { colors } = applyHighlight(arrays, [], null, [
      { ids: ['a'], style: { color: '#ff0000' } },
      { ids: ['a'], style: { color: '#0000ff' } },
    ])

    expect([...colors.slice(a * 4, a * 4 + 4)]).toEqual([0, 0, 1, 1])
  })

  test('★ AI style beats dim (a styled node shows through the filter)', () => {
    const arrays = arraysFor(['a', 'b'])
    const a = arrays.indexById.get('a')!

    // 'a' is not in the visible set (would dim) but is AI-styled green.
    const { colors } = applyHighlight(
      arrays,
      [],
      { visibleIds: new Set(['b']) },
      style(['a'], { color: '#00ff00' }),
    )

    expect([...colors.slice(a * 4, a * 4 + 4)]).toEqual([0, 1, 0, 1])
  })

  test('★ anchor beats AI style (gold on top)', () => {
    const arrays = arraysFor(['a'])
    const a = arrays.indexById.get('a')!

    // 'a' is AI-styled green AND a selected anchor → gold wins.
    const { colors } = applyHighlight(arrays, ['a'], null, style(['a'], { color: '#00ff00' }))

    const rgba = [...colors.slice(a * 4, a * 4 + 4)]
    expect(rgba[0]).toBeCloseTo(1) // amber, not green
    expect(rgba[1]).toBeCloseTo(0.85)
  })

  test('★ full precedence anchor > AI-style > dim on three nodes', () => {
    const arrays = arraysFor(['anchor', 'styled', 'dimmed'])
    const idx = (id: string) => arrays.indexById.get(id)!

    const { colors } = applyHighlight(
      arrays,
      ['anchor'],
      { visibleIds: new Set<string>() }, // nothing matches → all would dim
      style(['anchor', 'styled'], { color: '#00ff00' }),
    )

    // anchor → gold; styled → green (beats dim); dimmed → dim grey.
    expect(colors[idx('anchor') * 4]).toBeCloseTo(1)
    expect(colors[idx('anchor') * 4 + 1]).toBeCloseTo(0.85)
    expect([...colors.slice(idx('styled') * 4, idx('styled') * 4 + 4)]).toEqual([0, 1, 0, 1])
    expect(colors[idx('dimmed') * 4 + 3]).toBeLessThan(0.5) // dim alpha
  })

  test('a style targeting a missing id is skipped', () => {
    const arrays = arraysFor(['a'])
    const before = [...arrays.pointColors]

    const { colors } = applyHighlight(arrays, [], null, style(['ghost'], { color: '#fff' }))

    expect([...colors]).toEqual(before)
  })
})
