/**
 * Completion verification for TASK M3c — the temporal scrubber UI.
 *
 * Slider drag / play / live-return over a real graphStore. The overlay's rendering
 * is tested in temporal.test.ts and via the ★structureSeq guard (graphStore.test).
 */

import { act, fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, test } from 'vitest'
import { TemporalScrubber } from './TemporalScrubber'
import { useGraphStore } from '../stores/graphStore'
import { toMs } from '../views/temporal'
import type { Node, WSEvent } from '../types'

let seq = 0
function node(id: string, createdAt: string): Node {
  return {
    id, label: id, type: 'class', properties: {}, parent_id: null, style_hint: null,
    position_hint: null, layer: null, ttl: 0, tags: [], created_at: createdAt,
    updated_at: createdAt, created_by: null,
  }
}
function seed(nodes: Node[]) {
  act(() => {
    for (const n of nodes) {
      useGraphStore.getState().applyServerEvent({ op: 'node.add', data: n, seq: ++seq } as WSEvent)
    }
    useGraphStore.getState().flushNow()
  })
}

beforeEach(() => {
  seq = 0
  useGraphStore.getState().reset()
})

describe('TemporalScrubber ([M3c])', () => {
  test('stays hidden on an empty graph', () => {
    render(<TemporalScrubber />)
    expect(screen.queryByTestId('temporal-scrubber')).not.toBeInTheDocument()
  })

  test('renders a slider once the graph spans time', () => {
    seed([node('a', '2026-01-01T00:00:01Z'), node('b', '2026-01-01T00:00:09Z')])
    render(<TemporalScrubber />)
    expect(screen.getByTestId('temporal-scrubber')).toBeInTheDocument()
    expect(screen.getByTestId('temporal-slider')).toBeInTheDocument()
  })

  test('starts live, and dragging the slider holds the view at a past cutoff', () => {
    seed([node('a', '2026-01-01T00:00:01Z'), node('b', '2026-01-01T00:00:09Z')])
    render(<TemporalScrubber />)
    expect(screen.getByTestId('temporal-status')).toHaveTextContent('LIVE')
    expect(useGraphStore.getState().temporalCutoff).toBeNull()

    const mid = toMs('2026-01-01T00:00:05Z')
    act(() => {
      fireEvent.change(screen.getByTestId('temporal-slider'), { target: { value: String(mid) } })
    })
    expect(useGraphStore.getState().temporalCutoff).toBe(mid)
    expect(screen.getByTestId('temporal-status')).toHaveTextContent('과거')
  })

  test('dragging to the end returns to live (cutoff cleared)', () => {
    seed([node('a', '2026-01-01T00:00:01Z'), node('b', '2026-01-01T00:00:09Z')])
    render(<TemporalScrubber />)
    const slider = screen.getByTestId('temporal-slider')
    const max = toMs('2026-01-01T00:00:09Z')
    act(() => fireEvent.change(slider, { target: { value: String(toMs('2026-01-01T00:00:03Z')) } }))
    expect(useGraphStore.getState().temporalCutoff).not.toBeNull()
    act(() => fireEvent.change(slider, { target: { value: String(max) } }))
    expect(useGraphStore.getState().temporalCutoff).toBeNull() // at the end = live
  })

  test('the live-return button clears a past cutoff', () => {
    seed([node('a', '2026-01-01T00:00:01Z'), node('b', '2026-01-01T00:00:09Z')])
    render(<TemporalScrubber />)
    act(() => useGraphStore.getState().setTemporalCutoff(toMs('2026-01-01T00:00:04Z')))
    expect(screen.getByTestId('temporal-live')).toBeInTheDocument()
    act(() => fireEvent.click(screen.getByTestId('temporal-live')))
    expect(useGraphStore.getState().temporalCutoff).toBeNull()
    expect(screen.getByTestId('temporal-status')).toHaveTextContent('LIVE')
  })

  test('play toggles to pause', () => {
    seed([node('a', '2026-01-01T00:00:01Z'), node('b', '2026-01-01T00:00:09Z')])
    render(<TemporalScrubber />)
    const play = screen.getByTestId('temporal-play')
    expect(play).toHaveAttribute('aria-label', 'play')
    act(() => fireEvent.click(play))
    expect(screen.getByTestId('temporal-play')).toHaveAttribute('aria-label', 'pause')
  })
})
