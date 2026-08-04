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
import { temporalCounters, toMs } from '../views/temporal'
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

/**
 * ★ [15] (5) — 스크러버가 push 경로에 O(N+E) 를 두지 않는다 (TASK PERF1).
 *
 * 회귀가 **정확히 이 컴포넌트에서** 일어났다: bounds 를 useMemo(deps
 * [structureSeq]) 안에서 전체 스캔으로 구했고, 그래서 push 1회당 노드+엣지+
 * finding 전부를 Date.parse 로 훑는 일이 React render phase 안에서, cosmos
 * effect 앞에 벌어졌다([15 개정] 렌즈 B). 위 graphStore 테스트가 스토어 층을
 * 고정한다면 여기는 **컴포넌트가 그 층을 실제로 쓰는지**를 고정한다.
 */
describe('★ [15] (5) 스크러버의 트랙은 push 마다 그래프를 훑지 않는다', () => {
  function parsesForOnePush(graphSize: number): number {
    useGraphStore.getState().reset()
    seed(
      Array.from({ length: graphSize }, (_, i) =>
        node(`n${i}`, `2026-01-01T00:00:${String(i % 60).padStart(2, '0')}Z`),
      ),
    )
    const view = render(<TemporalScrubber />)
    temporalCounters.parses = 0

    seed([node('fresh', '2026-02-01T00:00:00Z')]) // 조용한 구간 뒤 단발 push
    view.rerender(<TemporalScrubber />)
    const spent = temporalCounters.parses
    view.unmount()
    return spent
  }

  test('렌더 1회당 파싱 수가 그래프 크기와 무관하다', () => {
    const at200 = parsesForOnePush(200)
    const at2000 = parsesForOnePush(2000)

    // 회귀 판이라면 201 대 2001 이었다 — 정확히 선형.
    expect(at2000, '스크러버가 push 마다 그래프 전체를 다시 훑는다').toBe(at200)
  })

  test('그래도 트랙은 새 데이터까지 늘어난다', () => {
    useGraphStore.getState().reset()
    seed([node('a', '2026-01-01T00:00:01Z'), node('b', '2026-01-01T00:00:09Z')])
    const view = render(<TemporalScrubber />)
    const before = Number(screen.getByTestId('temporal-slider').getAttribute('max'))

    seed([node('later', '2026-03-01T00:00:00Z')])
    view.rerender(<TemporalScrubber />)

    // 캐시가 낡아 트랙이 안 늘어나면 사용자는 새 구간으로 스크럽할 수 없다 —
    // 성능을 위해 정확성을 버린 셈이 된다.
    expect(Number(screen.getByTestId('temporal-slider').getAttribute('max'))).toBeGreaterThan(before)
    expect(Number(screen.getByTestId('temporal-slider').getAttribute('max')))
      .toBe(toMs('2026-03-01T00:00:00Z'))
  })
})
