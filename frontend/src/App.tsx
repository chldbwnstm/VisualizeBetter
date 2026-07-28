/**
 * App shell — [9-B] tree.
 *
 * The loop the project sells closes here: MCP pushes arrive over the WebSocket,
 * the overview draws them, clicking a finding flies the camera to its anchors and
 * lights them up, and the detail view slides in ([7-C]).
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { type Lang, useI18n, useStrings } from './i18n'
import { FindingsPanel } from './components/FindingsPanel'
import { NodeInspector } from './components/NodeInspector'
import { useGraphStore } from './stores/graphStore'
import { DetailPanel } from './views/DetailPanel'
import { OverviewCanvas } from './views/OverviewCanvas'
import { GraphClient } from './ws/client'
import { FilterBar } from './components/FilterBar'
import { SuggestionBanner } from './components/SuggestionBanner'
import { TemporalScrubber } from './components/TemporalScrubber'
import { AnnotationsLayer } from './components/AnnotationsLayer'
import { GraphErrorBoundary } from './components/GraphErrorBoundary'

function wsUrl(): string {
  const host = globalThis.location?.host ?? 'localhost:8765'
  return `ws://${host}/live`
}

function ConnectionBar({ connected }: { connected: boolean }) {
  const nodeCount = useGraphStore((s) => s.nodeCount)
  const edgeCount = useGraphStore((s) => s.edgeCount)
  const findings = useGraphStore((s) => s.findings)
  const viewMode = useGraphStore((s) => s.viewMode)
  const setViewMode = useGraphStore((s) => s.setViewMode)
  const lang = useI18n((s) => s.lang)
  const setLang = useI18n((s) => s.setLang)
  const t = useStrings()

  return (
    <header
      className="flex items-center gap-4 border-b border-slate-800 px-3 py-2 text-xs text-slate-400"
      data-testid="connection-bar"
    >
      <span className="font-semibold text-slate-200">VisualizeBetter</span>
      {/* [9-B] ConnectionBar: AI 접속 상태. Until this is live, a push has
          nowhere to arrive — which is exactly what a reader needs to know. */}
      <span
        data-testid="ws-status"
        data-connected={connected}
        className={connected ? 'text-emerald-400' : 'text-slate-600'}
      >
        {connected ? '● live' : '○ connecting'}
      </span>
      <span data-testid="node-count">nodes {nodeCount}</span>
      <span data-testid="edge-count">edges {edgeCount}</span>
      <span className="text-amber-300" data-testid="finding-count">
        findings {findings.size}
      </span>
      {/* 설정: UI 언어 (한국어/English). Chrome only — graph content is data. */}
      <select
        aria-label="language"
        title={t.languageLabel}
        data-testid="lang-select"
        value={lang}
        onChange={(e) => setLang(e.target.value as Lang)}
        className="ml-auto rounded border border-slate-700 bg-slate-900 px-1 py-0.5 text-[10px] text-slate-300"
      >
        <option value="ko">한국어</option>
        <option value="en">English</option>
      </select>
      {/* [7-C] 동시 표시: 화면 2분할 모드 (설정 가능) — [9-C] viewMode. */}
      <label className="flex items-center gap-1" data-testid="split-toggle">
        <input
          type="checkbox"
          aria-label="split view"
          checked={viewMode === 'split'}
          onChange={(e) => setViewMode(e.target.checked ? 'split' : 'overview')}
        />
        {t.splitView}
      </label>
    </header>
  )
}

export interface AppProps {
  /** Pass null to mount without opening a socket (tests). */
  client?: GraphClient | null
}

export default function App({ client }: AppProps = {}) {
  const t = useStrings()
  const clientRef = useRef<GraphClient | null>(null)
  const focus = useGraphStore((s) => s.focus)
  const viewMode = useGraphStore((s) => s.viewMode)
  const [detailOpen, setDetailOpen] = useState(false)
  const [anchors, setAnchors] = useState<readonly string[]>([])
  const [expandRequests, setExpandRequests] = useState<string[]>([])
  const [connected, setConnected] = useState(false)

  // [7-C]: split shows both at once; otherwise detail is a slide-in over focus.
  const showDetail = Boolean(focus) && (viewMode === 'split' || detailOpen)

  useEffect(() => {
    if (client === null) return
    const active = client ?? new GraphClient({ url: wsUrl() })
    clientRef.current = active
    active.onConnectionChange = setConnected
    active.connect()
    return () => {
      active.onConnectionChange = undefined
      active.close()
      clientRef.current = null
    }
  }, [client])

  // [5-D] focus_on: a server focus.set (AI navigation) opens the detail view.
  // Fires on focus *change* only, so closing detail without a new focus does not
  // reopen it. A human click also sets focus and opens detail locally; this
  // covers the case where the AI, not the human, moved the view.
  useEffect(() => {
    if (focus) setDetailOpen(true)
  }, [focus])

  // [7-C] Overview → Detail: 노드 클릭 → detail 슬라이드인.
  const selectNode = useCallback((id: string) => {
    useGraphStore.getState().setFocus(id)
    clientRef.current?.sendFocusSet(id)
    setDetailOpen(true)
  }, [])

  /**
   * [23-F] TASK 7: finding 클릭 → 해당 subgraph 로 카메라 이동 + 앵커 강조.
   * The anchors drive the overlay; no node or edge is created ([23-B]).
   */
  const focusFinding = useCallback((anchorIds: readonly string[]) => {
    setAnchors(anchorIds)
    const first = anchorIds[0]
    if (!first) return
    useGraphStore.getState().setFocus(first)
    clientRef.current?.sendFocusSet(first)
    setDetailOpen(true)
  }, [])

  /**
   * [7-B] "확장" — ask the AI for more of this node's neighbourhood.
   *
   * There is no transport for it yet: [8-C]'s Client→Server ops do not include an
   * expand request, and [5-C] poll_events (how an AI would notice) is unexposed.
   * Adding either is a protocol/tool change needing approval. Until then the
   * request is recorded and surfaced, and focus.set at least tells the AI which
   * node the human is on — that much [8-C] already carries.
   */
  const requestExpand = useCallback((nodeId: string) => {
    setExpandRequests((current) => [...new Set([...current, nodeId])])
    clientRef.current?.sendFocusSet(nodeId)
  }, [])

  // [5-C] The human's filter goes to the server, which evaluates the [6] DSL and
  // broadcasts the visible set — the shared view. The store applies the result.
  const applyFilter = useCallback((expression: string) => {
    clientRef.current?.sendFilterSet(expression)
  }, [])

  // [M2e] Undo/redo the last graph mutation. The server reverses it and
  // re-broadcasts the node/edge/finding events, so the view follows over [8-C] —
  // there is no local optimistic change to make here. The shared history means a
  // human can undo an action an AI just made (single shared graph, M2 by design).
  const undo = useCallback(() => {
    clientRef.current?.sendUndo()
  }, [])
  const redo = useCallback(() => {
    clientRef.current?.sendRedo()
  }, [])

  return (
    <div className="flex h-screen flex-col bg-slate-950 text-slate-100">
      <ConnectionBar connected={connected} />
      <SuggestionBanner onApply={applyFilter} />
      <div
        className="flex items-center gap-3 border-b border-slate-800 px-3 py-1.5"
        data-testid="filter-row"
      >
        <FilterBar onApply={applyFilter} />
        {/* [M3c] time-axis scrubber — replay the graph's growth by created_at. */}
        <TemporalScrubber />
        {/* [M2e] undo/redo the last graph mutation (shared session history). */}
        <div className="ml-auto flex items-center gap-1" data-testid="history-controls">
          <button
            type="button"
            data-testid="undo-button"
            aria-label="undo"
            title={t.undoTitle}
            onClick={undo}
            className="rounded border border-slate-700 px-2 py-1 text-xs text-slate-300 hover:bg-slate-800"
          >
            ↶ undo
          </button>
          <button
            type="button"
            data-testid="redo-button"
            aria-label="redo"
            title={t.redoTitle}
            onClick={redo}
            className="rounded border border-slate-700 px-2 py-1 text-xs text-slate-300 hover:bg-slate-800"
          >
            ↷ redo
          </button>
        </div>
      </div>
      <div className="flex min-h-0 flex-1">
        <main className="relative min-w-0 flex-1" data-testid="viewport">
          {/* [B] A cosmos.gl crash here must not blank the whole app ([KI-1]). */}
          <GraphErrorBoundary label="overview">
            <OverviewCanvas highlighted={anchors} onSelectNode={selectNode} />
          </GraphErrorBoundary>
          {/* [5-D] AI screen-space notes, over the graph, pointer-events-none. */}
          <AnnotationsLayer />
        </main>
        {showDetail && (
          <GraphErrorBoundary label="detail">
            <DetailPanel
              highlighted={anchors}
              onClose={() => {
                setDetailOpen(false)
                // In split mode the panel is pinned; closing means leaving split.
                if (viewMode === 'split') useGraphStore.getState().setViewMode('overview')
              }}
              onRequestExpand={requestExpand}
            />
          </GraphErrorBoundary>
        )}
        <aside
          className="flex w-80 shrink-0 flex-col divide-y divide-slate-800 overflow-y-auto border-l border-slate-800"
          data-testid="right-sidebar"
        >
          <FindingsPanel onFocusFinding={focusFinding} />
          <NodeInspector onSelectNeighbor={selectNode} />
          {expandRequests.length > 0 && (
            <section className="p-3" data-testid="expand-requests">
              <h3 className="text-[10px] uppercase tracking-wide text-slate-500">
                {t.expandRequestsHeader}
              </h3>
              <ul className="text-xs text-slate-300">
                {expandRequests.map((id) => (
                  <li key={id} data-testid="expand-request">
                    {id}
                  </li>
                ))}
              </ul>
            </section>
          )}
        </aside>
      </div>
    </div>
  )
}
