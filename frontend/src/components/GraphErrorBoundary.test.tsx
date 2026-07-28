/**
 * [KI-1 / B] The error boundary must contain a graph-view crash — keep the rest
 * of the app mounted — and auto-remount to recover, bounded so a permanently
 * broken view cannot spin.
 */

import { act, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest'
import { GraphErrorBoundary } from './GraphErrorBoundary'

/**
 * Throws while `shouldThrow` is set. A boolean, not a per-render counter: React 19
 * may invoke a component more than once per commit (a concurrent attempt then a
 * synchronous retry), so a decrementing counter would "recover" on the retry and
 * never reach the boundary. Flipping the flag models the transient cause clearing.
 */
let shouldThrow = false
function Flaky() {
  if (shouldThrow) throw new Error('simulated graph-view crash')
  return <div data-testid="flaky-ok">ok</div>
}

beforeEach(() => {
  shouldThrow = false
  vi.useFakeTimers()
  // React logs a caught render error to console.error; silence it for clean output.
  vi.spyOn(console, 'error').mockImplementation(() => {})
})

afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe('GraphErrorBoundary', () => {
  test('a crash is contained — the rest of the app stays mounted', () => {
    shouldThrow = true // stays crashed, so we observe the contained state
    render(
      <div>
        <div data-testid="sibling">alive</div>
        <GraphErrorBoundary label="test">
          <Flaky />
        </GraphErrorBoundary>
      </div>,
    )

    // The throw did not propagate to the root: the sibling (≈ the rest of the app,
    // header/connection bar/sidebars) is still there, and the view shows a fallback.
    expect(screen.getByTestId('sibling')).toBeTruthy()
    expect(screen.getByTestId('graph-error-fallback')).toBeTruthy()
    expect(screen.queryByTestId('flaky-ok')).toBeNull()
  })

  test('auto-remounts and recovers when the crash was transient', () => {
    shouldThrow = true // crash on first mount
    render(
      <GraphErrorBoundary label="test">
        <Flaky />
      </GraphErrorBoundary>,
    )
    expect(screen.queryByTestId('flaky-ok')).toBeNull() // crashed on first mount

    shouldThrow = false // the transient cause clears before the remount
    act(() => {
      vi.runAllTimers() // the scheduled remount fires
    })

    expect(screen.getByTestId('flaky-ok')).toBeTruthy() // recovered
  })

  test('stops retrying after the bound and holds a persistent fallback', () => {
    shouldThrow = true // always crashes
    render(
      <GraphErrorBoundary label="test">
        <Flaky />
      </GraphErrorBoundary>,
    )

    // Pump one round at a time: each remount re-crashes and schedules the next,
    // and the act() flushes that re-render before the following tick fires it.
    for (let round = 0; round < 6; round += 1) {
      act(() => {
        vi.advanceTimersByTime(1)
      })
    }

    // Exhausted retries → the notice, not an infinite remount loop.
    expect(screen.getByTestId('graph-error-fallback').textContent).toContain(
      '불러오지 못했습니다',
    )
  })
})
