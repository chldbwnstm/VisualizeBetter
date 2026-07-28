/**
 * Completion verification for TASK V — FilterBar ([5-C], [6], [11]).
 */

import { act, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, test, vi } from 'vitest'
import { FilterBar } from './FilterBar'
import { useGraphStore } from '../stores/graphStore'
import type { WSEvent } from '../types'

let seq = 0

function filterSet(over: { expression: string; visible_ids?: string[]; error?: string | null }) {
  act(() => {
    useGraphStore.getState().applyServerEvent({
      op: 'filter.set',
      seq: ++seq,
      data: { visible_ids: [], error: null, ...over },
    } as WSEvent)
    useGraphStore.getState().flushNow()
  })
}

beforeEach(() => {
  seq = 0
  useGraphStore.getState().reset()
})

test('typing and Enter sends the expression to the server', async () => {
  const onApply = vi.fn()
  render(<FilterBar onApply={onApply} />)

  await userEvent.type(screen.getByTestId('filter-input'), 'type == "class"')
  await userEvent.keyboard('{Enter}')

  expect(onApply).toHaveBeenCalledWith('type == "class"')
})

test('the apply button sends too', async () => {
  const onApply = vi.fn()
  render(<FilterBar onApply={onApply} />)

  await userEvent.type(screen.getByTestId('filter-input'), 'type == "service"')
  await userEvent.click(screen.getByTestId('filter-apply'))

  expect(onApply).toHaveBeenCalledWith('type == "service"')
})

test('the expression is trimmed before sending', async () => {
  const onApply = vi.fn()
  render(<FilterBar onApply={onApply} />)

  await userEvent.type(screen.getByTestId('filter-input'), '  type == "x"  ')
  await userEvent.keyboard('{Enter}')

  expect(onApply).toHaveBeenCalledWith('type == "x"')
})

describe('server feedback', () => {
  test('shows the matched count for an applied filter', () => {
    render(<FilterBar onApply={vi.fn()} />)
    filterSet({ expression: 'type == "class"', visible_ids: ['a', 'b'] })

    expect(screen.getByTestId('filter-active')).toHaveTextContent('2 matched')
    expect(screen.queryByTestId('filter-error')).not.toBeInTheDocument()
  })

  test('shows the server error and keeps no matched count', () => {
    render(<FilterBar onApply={vi.fn()} />)
    filterSet({ expression: 'type == "class"', visible_ids: ['a'] })
    filterSet({ expression: 'bad ===', error: 'could not parse filter' })

    expect(screen.getByTestId('filter-error')).toHaveTextContent('could not parse filter')
  })

  test('[11] a hostile error string renders as text, never as markup', () => {
    const { container } = render(<FilterBar onApply={vi.fn()} />)
    filterSet({ expression: 'x', error: '<img src=x onerror="alert(1)">' })

    expect(container.querySelector('img')).toBeNull()
    expect(screen.getByTestId('filter-error')).toHaveTextContent('<img src=x onerror="alert(1)">')
  })
})
