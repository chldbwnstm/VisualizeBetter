/**
 * Completion verification for TASK Y — AnnotationsLayer + [5-D] TTL ([11]).
 */

import { act, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest'
import { AnnotationsLayer } from './AnnotationsLayer'
import { useGraphStore } from '../stores/graphStore'
import type { WSEvent } from '../types'

let seq = 0

function annotate(id: string, x: number, y: number, text: string, ttl = 0) {
  act(() => {
    useGraphStore.getState().applyServerEvent({
      op: 'annotation.add',
      seq: ++seq,
      data: { annotation_id: id, x, y, text, ttl },
    } as WSEvent)
    useGraphStore.getState().flushNow()
  })
}

beforeEach(() => {
  seq = 0
  useGraphStore.getState().reset()
})

test('renders nothing when there are no annotations', () => {
  render(<AnnotationsLayer />)
  expect(screen.queryByTestId('annotations-layer')).not.toBeInTheDocument()
})

test('shows a note at its coordinates', () => {
  render(<AnnotationsLayer />)
  annotate('n1', 30, 40, '여기 보세요')

  const note = screen.getByTestId('annotation')
  expect(note).toHaveTextContent('여기 보세요')
  expect(note).toHaveStyle({ left: '30px', top: '40px' })
})

test('★ [11] annotation text renders as text, never as markup', () => {
  const { container } = render(<AnnotationsLayer />)
  annotate('n1', 0, 0, '<img src=x onerror="alert(1)">')

  expect(container.querySelector('img')).toBeNull()
  expect(screen.getByTestId('annotation')).toHaveTextContent('<img src=x onerror="alert(1)">')
})

test('shows multiple annotations', () => {
  render(<AnnotationsLayer />)
  annotate('n1', 0, 0, 'one')
  annotate('n2', 10, 10, 'two')

  expect(screen.getAllByTestId('annotation')).toHaveLength(2)
})

describe('TTL expiry ([5-D])', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => vi.useRealTimers())

  test('a note with a ttl removes itself when it elapses', () => {
    render(<AnnotationsLayer />)
    annotate('n1', 0, 0, 'temporary', 3)
    expect(screen.getByTestId('annotation')).toBeInTheDocument()

    act(() => vi.advanceTimersByTime(3000))

    expect(screen.queryByTestId('annotation')).not.toBeInTheDocument()
  })

  test('a note with ttl 0 persists', () => {
    render(<AnnotationsLayer />)
    annotate('n1', 0, 0, 'permanent', 0)

    act(() => vi.advanceTimersByTime(60_000))

    expect(screen.getByTestId('annotation')).toBeInTheDocument()
  })
})
