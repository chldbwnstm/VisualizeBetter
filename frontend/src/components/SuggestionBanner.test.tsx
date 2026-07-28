/**
 * Completion verification for TASK X — SuggestionBanner + [5-D] store wiring.
 */

import { act, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, test, vi } from 'vitest'
import { SuggestionBanner } from './SuggestionBanner'
import { useGraphStore } from '../stores/graphStore'
import type { WSEvent } from '../types'

let seq = 0

function push(event: Partial<WSEvent> & { op: string; data: unknown }) {
  act(() => {
    useGraphStore.getState().applyServerEvent({ seq: ++seq, ...event } as WSEvent)
    useGraphStore.getState().flushNow()
  })
}

function suggest(expression: string, reason: string) {
  push({ op: 'filter.suggest', data: { expression, reason } })
}

beforeEach(() => {
  seq = 0
  useGraphStore.getState().reset()
})

// --- store wiring ([5-D]) ---

describe('store [5-D] events', () => {
  test('filter.suggest becomes a pending suggestion', () => {
    suggest('type == "class"', 'these are classes')
    expect(useGraphStore.getState().suggestion).toEqual({
      expression: 'type == "class"',
      reason: 'these are classes',
    })
  })

  test('layout.set becomes the active layout', () => {
    push({ op: 'layout.set', data: { algorithm: 'dagre', options: {} } })
    expect(useGraphStore.getState().activeLayout).toBe('dagre')
  })

  test('dismissSuggestion clears it', () => {
    suggest('type == "x"', 'r')
    act(() => useGraphStore.getState().dismissSuggestion())
    expect(useGraphStore.getState().suggestion).toBeNull()
  })
})

// --- banner component ([5-D], [11]) ---

describe('SuggestionBanner', () => {
  test('renders nothing when there is no suggestion', () => {
    render(<SuggestionBanner onApply={vi.fn()} />)
    expect(screen.queryByTestId('suggestion-banner')).not.toBeInTheDocument()
  })

  test('shows the expression and reason', () => {
    render(<SuggestionBanner onApply={vi.fn()} />)
    suggest('type == "service"', '서비스만 보기')

    expect(screen.getByTestId('suggestion-expression')).toHaveTextContent('type == "service"')
    expect(screen.getByTestId('suggestion-reason')).toHaveTextContent('서비스만 보기')
  })

  test('★ [적용] applies the expression and closes the banner', async () => {
    const onApply = vi.fn()
    render(<SuggestionBanner onApply={onApply} />)
    suggest('type == "class"', 'r')

    await userEvent.click(screen.getByTestId('suggestion-apply'))

    // Applies via the shared-filter path (TASK V), then dismisses.
    expect(onApply).toHaveBeenCalledWith('type == "class"')
    expect(screen.queryByTestId('suggestion-banner')).not.toBeInTheDocument()
    expect(useGraphStore.getState().suggestion).toBeNull()
  })

  test('[무시] closes without applying', async () => {
    const onApply = vi.fn()
    render(<SuggestionBanner onApply={onApply} />)
    suggest('type == "class"', 'r')

    await userEvent.click(screen.getByTestId('suggestion-dismiss'))

    expect(onApply).not.toHaveBeenCalled()
    expect(screen.queryByTestId('suggestion-banner')).not.toBeInTheDocument()
  })

  test('★ [11] AI-written expression and reason render as text, never as markup', () => {
    const { container } = render(<SuggestionBanner onApply={vi.fn()} />)
    suggest('<img src=x onerror="alert(1)">', '<script>bad()</script>')

    expect(container.querySelector('img')).toBeNull()
    expect(container.querySelector('script')).toBeNull()
    expect(screen.getByTestId('suggestion-expression')).toHaveTextContent(
      '<img src=x onerror="alert(1)">',
    )
    expect(screen.getByTestId('suggestion-reason')).toHaveTextContent('<script>bad()</script>')
  })

  test('a new suggestion replaces the previous one', () => {
    render(<SuggestionBanner onApply={vi.fn()} />)
    suggest('type == "a"', 'first')
    suggest('type == "b"', 'second')

    expect(screen.getByTestId('suggestion-expression')).toHaveTextContent('type == "b"')
  })
})
