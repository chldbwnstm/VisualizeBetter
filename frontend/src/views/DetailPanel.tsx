/**
 * DetailPanel — cytoscape focus view ([7-B], [7-C]).
 *
 * Shows the focused node plus its 2-hop neighbourhood with labels, arrows and
 * relation text. Layout tabs mirror [5-D]'s set_layout enum exactly, so the AI
 * and the human name the same layouts.
 *
 * ★ Labels are cytoscape's native canvas text, not cytoscape-node-html-label.
 * [11] calls node-html-label out by name as an HTML render path needing sanitize,
 * and every label here is AI/import-supplied. Canvas text cannot execute markup,
 * so the injection path is removed rather than guarded. node-html-label is not in
 * the [17] locked stack (that pins cytoscape +dagre +fcose), so nothing is
 * violated by leaving it out — it can return when a node genuinely needs rich
 * HTML, with sanitize attached.
 */

import { useEffect, useMemo, useRef, useState } from 'react'
import cytoscape from 'cytoscape'
import dagre from 'cytoscape-dagre'
import fcose from 'cytoscape-fcose'
import { useStrings } from '../i18n'
import { graphData, useGraphStore } from '../stores/graphStore'
import { rgbToCss, typeColor } from './palette'
import { extractSubgraph } from './subgraph'
import { visibleNodeIds } from './temporal'

cytoscape.use(dagre)
cytoscape.use(fcose)

/** [7-B]/[5-D] set_layout enum 과 동일 목록. */
export const LAYOUTS = ['dagre', 'concentric', 'fcose', 'grid', 'preset'] as const
export type LayoutName = (typeof LAYOUTS)[number]

export interface DetailPanelProps {
  onClose?: () => void
  /** Anchors of the selected finding, highlighted here too ([23-F] TASK 7). */
  highlighted?: readonly string[]
  /**
   * [7-B] "확장" 버튼 → AI 에게 "이 노드 이웃 더 push 해줘".
   *
   * Seam, not a wire call. [8-C]'s Client→Server list is focus.set / filter.set /
   * layer.toggle / layout.set / view.update — there is no op for "expand this",
   * and [5-C] poll_events (where such a request would surface to the AI) is not
   * exposed yet. Adding either is a protocol/tool change needing approval, so the
   * button raises it here and the transport lands with that task.
   */
  onRequestExpand?: (nodeId: string) => void
}

interface ContextMenuState {
  nodeId: string
  x: number
  y: number
}

interface HoverState {
  label: string
  type: string
  x: number
  y: number
}

export function toCytoscapeElements(
  subgraph: ReturnType<typeof extractSubgraph>,
  focus: string | null,
  highlighted: readonly string[],
  dim?: { visibleIds: ReadonlySet<string> } | null,
): cytoscape.ElementDefinition[] {
  const anchors = new Set(highlighted)
  const elements: cytoscape.ElementDefinition[] = subgraph.nodes.map((node) => {
    // [5-C] filter dim, same precedence as the overview: an anchor or the focused
    // node is never dimmed — an explicit selection wins over the shared filter.
    const dimmed =
      dim != null &&
      !dim.visibleIds.has(node.id) &&
      node.id !== focus &&
      !anchors.has(node.id)
    return {
      data: {
        id: node.id,
        label: node.label,
        color: rgbToCss(typeColor(node.type)),
      },
      classes: [
        node.id === focus ? 'focused' : '',
        anchors.has(node.id) ? 'anchor' : '',
        node.properties?.placeholder === true ? 'placeholder' : '',
        dimmed ? 'dimmed' : '',
      ]
        .filter(Boolean)
        .join(' '),
    }
  })

  const isDimmed = (id: string) =>
    dim != null && !dim.visibleIds.has(id) && id !== focus && !anchors.has(id)
  for (const edge of subgraph.edges) {
    // An edge fades with its endpoints: if either end is dimmed, the edge is too.
    const dimmed = isDimmed(edge.source) || isDimmed(edge.target)
    elements.push({
      data: {
        id: `${edge.source}|${edge.target}|${edge.relation}|${edge.key}`,
        source: edge.source,
        target: edge.target,
        label: edge.relation,
      },
      classes: [edge.directed ? 'directed' : '', dimmed ? 'dimmed' : ''].filter(Boolean).join(' '),
    })
  }
  return elements
}

const STYLE: cytoscape.StylesheetJson = [
  {
    selector: 'node',
    style: {
      'background-color': 'data(color)',
      // Native canvas label — markup in a label can never execute ([11]).
      label: 'data(label)',
      color: '#e2e8f0',
      'font-size': 10,
      'text-valign': 'bottom',
      'text-margin-y': 4,
      width: 20,
      height: 20,
    },
  },
  { selector: 'node.placeholder', style: { 'border-style': 'dashed', 'border-width': 2, 'border-color': '#64748b', opacity: 0.7 } },
  // [5-C] filter dim — after placeholder so a dimmed placeholder still recedes.
  { selector: 'node.dimmed', style: { opacity: 0.2 } },
  { selector: 'edge.dimmed', style: { opacity: 0.15 } },
  { selector: 'node.focused', style: { 'border-width': 3, 'border-color': '#38bdf8' } },
  { selector: 'node.anchor', style: { 'background-color': '#fbbf24', width: 28, height: 28 } },
  {
    selector: 'edge',
    style: {
      width: 1.5,
      'line-color': '#475569',
      label: 'data(label)',
      'font-size': 8,
      color: '#94a3b8',
      'curve-style': 'bezier',
    },
  },
  {
    selector: 'edge.directed',
    style: { 'target-arrow-shape': 'triangle', 'target-arrow-color': '#475569' },
  },
]

export function DetailPanel({
  onClose,
  highlighted = [],
  onRequestExpand,
}: DetailPanelProps) {
  const t = useStrings()
  const container = useRef<HTMLDivElement>(null)
  const cy = useRef<cytoscape.Core | null>(null)
  const [layout, setLayout] = useState<LayoutName>('fcose')
  const [menu, setMenu] = useState<ContextMenuState | null>(null)
  const [hovered, setHovered] = useState<HoverState | null>(null)
  const [hidden, setHidden] = useState<Set<string>>(new Set())

  const focus = useGraphStore((s) => s.focus)
  const seq = useGraphStore((s) => s.seq)
  const setFocus = useGraphStore((s) => s.setFocus)
  // [5-D] the AI can set the detail layout (set_layout → layout.set). It writes
  // the same local state the tabs do, so an AI choice and a click are equivalent.
  const activeLayout = useGraphStore((s) => s.activeLayout)
  useEffect(() => {
    if (activeLayout && (LAYOUTS as readonly string[]).includes(activeLayout)) {
      setLayout(activeLayout as LayoutName)
    }
  }, [activeLayout])
  // [5-C] shared filter — dims non-matching nodes in the detail view too.
  const filter = useGraphStore((s) => s.filter)
  // [M3c] temporal scrubber composes with the filter: a node is shown only if it
  // both passes the filter AND was created at or before the cutoff (intersection);
  // an edge fades with its endpoints, so future edges recede automatically.
  const temporalCutoff = useGraphStore((s) => s.temporalCutoff)
  const dim = useMemo(() => {
    const temporal = visibleNodeIds(graphData.nodes, temporalCutoff)
    const filterActive = filter.expression.trim().length > 0
    if (!filterActive && !temporal) return null
    if (!temporal) return { visibleIds: filter.visibleIds }
    if (!filterActive) return { visibleIds: temporal }
    const both = new Set<string>()
    for (const id of temporal) if (filter.visibleIds.has(id)) both.add(id)
    return { visibleIds: both }
  }, [filter, temporalCutoff, seq])

  const subgraph = useMemo(
    () => extractSubgraph(graphData.nodes, graphData.edges, focus),
    [focus, seq],
  )

  useEffect(() => {
    if (!container.current || cy.current) return
    const core = cytoscape({ container: container.current, style: STYLE, elements: [] })
    cy.current = core

    core.on('tap', 'node', (event) => setFocus(event.target.id()))
    // [7-B] 클릭 → 인스펙터 갱신 (setFocus above); 우클릭 → 컨텍스트 메뉴.
    core.on('cxttap', 'node', (event) => {
      const { x, y } = event.renderedPosition ?? { x: 0, y: 0 }
      setMenu({ nodeId: event.target.id(), x, y })
    })
    core.on('tap', (event) => {
      if (event.target === core) setMenu(null)
    })
    // [7-B] hover → 툴팁.
    core.on('mouseover', 'node', (event) => {
      const node = graphData.getNode(event.target.id())
      if (!node) return
      const { x, y } = event.renderedPosition ?? { x: 0, y: 0 }
      setHovered({ label: node.label, type: node.type, x, y })
    })
    core.on('mouseout', 'node', () => setHovered(null))
    // [7-B] 드래그 → 노드 재배치 (사용자 수동). Switch to preset so the next
    // relayout does not yank the node back where the algorithm wants it.
    core.on('dragfree', 'node', () => setLayout('preset'))

    return () => {
      core.destroy()
      cy.current = null
    }
  }, [setFocus])

  useEffect(() => {
    const core = cy.current
    if (!core) return
    const visible = {
      ...subgraph,
      nodes: subgraph.nodes.filter((n) => !hidden.has(n.id)),
      edges: subgraph.edges.filter((e) => !hidden.has(e.source) && !hidden.has(e.target)),
    }
    core.elements().remove()
    core.add(toCytoscapeElements(visible, focus, highlighted, dim))
    if (layout !== 'preset') {
      core.layout({ name: layout, animate: false } as cytoscape.LayoutOptions).run()
    }
    core.fit(undefined, 30)
  }, [subgraph, focus, highlighted, layout, hidden, dim])

  // A new focus is a new neighbourhood — old hides should not linger.
  useEffect(() => setHidden(new Set()), [focus])

  return (
    <section
      className="flex h-full w-[28rem] shrink-0 flex-col border-l border-slate-800 bg-slate-950"
      data-testid="detail-panel"
    >
      <header className="flex items-center gap-2 border-b border-slate-800 px-3 py-2">
        <h2 className="flex-1 truncate text-xs font-semibold text-slate-200" data-testid="detail-focus">
          {focus}
        </h2>
        <button
          type="button"
          onClick={onClose}
          className="text-[10px] text-slate-400 hover:text-slate-200"
          data-testid="detail-close"
        >
          {t.showMap}
        </button>
      </header>
      <nav className="flex gap-1 border-b border-slate-800 px-2 py-1" data-testid="layout-tabs">
        {LAYOUTS.map((name) => (
          <button
            key={name}
            type="button"
            aria-pressed={layout === name}
            onClick={() => setLayout(name)}
            className={`rounded px-2 py-0.5 text-[10px] ${
              layout === name ? 'bg-sky-600 text-white' : 'text-slate-400 hover:bg-slate-800'
            }`}
          >
            {name}
          </button>
        ))}
      </nav>
      {subgraph.truncated && (
        <p className="px-3 py-1 text-[10px] text-amber-400" data-testid="detail-truncated">
          {t.neighborsTruncated}
        </p>
      )}
      <div className="relative min-h-0 flex-1">
        <div ref={container} className="h-full w-full" data-testid="cytoscape-container" />

        {hovered && (
          <div
            className="pointer-events-none absolute z-10 -translate-x-1/2 -translate-y-full rounded bg-slate-800 px-2 py-1 text-[10px] text-slate-100 shadow-lg"
            style={{ left: hovered.x, top: hovered.y - 8 }}
            data-testid="detail-tooltip"
          >
            <span className="font-semibold">{hovered.label}</span>
            <span className="ml-1 text-slate-400">{hovered.type}</span>
          </div>
        )}

        {/* [7-B] 우클릭 → 컨텍스트 메뉴 (숨기기, 확장 요청, 노트 추가). */}
        {menu && (
          <div
            className="absolute z-20 min-w-32 rounded border border-slate-700 bg-slate-900 py-1 text-[11px] shadow-xl"
            style={{ left: menu.x, top: menu.y }}
            data-testid="context-menu"
          >
            <button
              type="button"
              data-testid="menu-hide"
              className="block w-full px-3 py-1 text-left text-slate-200 hover:bg-slate-800"
              onClick={() => {
                setHidden((current) => new Set(current).add(menu.nodeId))
                setMenu(null)
              }}
            >
              {t.hideNode}
            </button>
            <button
              type="button"
              data-testid="menu-expand"
              className="block w-full px-3 py-1 text-left text-slate-200 hover:bg-slate-800"
              onClick={() => {
                onRequestExpand?.(menu.nodeId)
                setMenu(null)
              }}
            >
              {t.requestExpand}
            </button>
            <button
              type="button"
              data-testid="menu-note"
              disabled
              title={t.addNoteTitle}
              className="block w-full cursor-not-allowed px-3 py-1 text-left text-slate-600"
            >
              {t.addNote}
            </button>
          </div>
        )}
      </div>
    </section>
  )
}
