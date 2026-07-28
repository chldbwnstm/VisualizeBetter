/**
 * [KI-1 / B] Isolate a graph view (the cosmos.gl overview, the cytoscape detail)
 * so a render/effect throw inside it cannot unmount the whole app.
 *
 * Before this, the app had no error boundary: a single throw during commit — e.g.
 * a cosmos.gl GPU-init race — propagated to the React root and unmounted
 * everything, leaving the user a blank page (this is what made KI-1 fatal rather
 * than a flicker). A boundary around each graph view turns that into a contained,
 * self-healing blip: the crash is caught, the subtree is remounted on the next
 * tick (a fresh renderer, which the readiness gate then feeds only once ready),
 * and the rest of the app — header, connection state, sidebars — stays live.
 *
 * Auto-remount is bounded so a genuinely broken view cannot spin forever; after
 * MAX_AUTO_REMOUNTS it holds a static fallback instead.
 */

import { Component, type ErrorInfo, type ReactNode } from 'react'
import { getStrings } from '../i18n'

const MAX_AUTO_REMOUNTS = 3

interface Props {
  children: ReactNode
  /** Names the view in logs/telemetry, e.g. "overview" / "detail". */
  label?: string
}

interface State {
  crashed: boolean
  /** Remount counter — also the child key, so bumping it forces a fresh mount. */
  remounts: number
}

export class GraphErrorBoundary extends Component<Props, State> {
  state: State = { crashed: false, remounts: 0 }
  private timer: ReturnType<typeof setTimeout> | undefined

  static getDerivedStateFromError(): Partial<State> {
    return { crashed: true }
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // Fires on every catch — the initial-mount crash (where componentDidMount, not
    // componentDidUpdate, runs) as well as a crash on remount — so this is where
    // the bounded auto-remount is scheduled.
    // eslint-disable-next-line no-console
    console.error(
      `[GraphErrorBoundary${this.props.label ? `:${this.props.label}` : ''}] view crashed; recovering`,
      error,
      info.componentStack,
    )
    if (this.state.remounts < MAX_AUTO_REMOUNTS) {
      if (this.timer !== undefined) clearTimeout(this.timer)
      // Next tick, not synchronously: let the failed commit unwind first, then
      // bump the child key to remount a clean subtree.
      this.timer = setTimeout(() => {
        this.setState((s) => ({ crashed: false, remounts: s.remounts + 1 }))
      }, 0)
    }
  }

  componentWillUnmount(): void {
    if (this.timer !== undefined) clearTimeout(this.timer)
  }

  render(): ReactNode {
    if (this.state.crashed) {
      // Exhausted retries → a persistent notice; mid-recovery → a transparent
      // placeholder that occupies the view's box without shifting layout.
      return (
        <div
          data-testid="graph-error-fallback"
          className="flex h-full w-full items-center justify-center text-xs text-slate-500"
        >
          {this.state.remounts >= MAX_AUTO_REMOUNTS ? getStrings().viewLoadFailed : null}
        </div>
      )
    }
    // The key is the remount counter: after a crash it changes, so React unmounts
    // the crashed subtree and mounts a fresh one (new renderer, clean state).
    return <ChildKey key={this.state.remounts}>{this.props.children}</ChildKey>
  }
}

/** A keyed passthrough — keeps the boundary from adding a layout box of its own. */
function ChildKey({ children }: { children: ReactNode }) {
  return <>{children}</>
}
