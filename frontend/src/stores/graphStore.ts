/**
 * Graph store ([9-C]) + the [7-D] update strategy.
 *
 * [7-D] is an M1 hard requirement, not a preference: "lag 없음" is bought by this
 * batching, not by the framework (D2). Two rules follow, and both are load-bearing:
 *
 *  1. **Bulk node/edge bodies live outside React.** They sit in module-scope Maps
 *     and are handed to cosmos.gl directly; the store keeps only lightweight UI
 *     state — counts, filter, focus, findings, layers ([9-C]'s note explicitly
 *     permits this split). React components subscribe to aggregates, never to
 *     individual node data.
 *  2. **Incoming events are queued and flushed once per rAF (or 50ms).** An event
 *     never touches the store directly — 1000 push/s ([15]) with a store write per
 *     event is exactly the failure [7-D] forbids.
 */

import { create } from 'zustand'
import { TimelineTracker, type TimelineBounds } from '../views/temporal'
import {
  type ClearData,
  type Edge,
  type EdgeIdentityData,
  type EdgeKey,
  type EdgeUpdateData,
  type Finding,
  type LayerInfo,
  type Node,
  type NodeUpdateData,
  type Patch,
  type StyleValue,
  type ViewMode,
  type WSEvent,
  edgeKey,
  edgeKeyOf,
} from '../types'

// --- off-React bulk data ([7-D]) ---

const nodes = new Map<string, Node>()
const edges = new Map<EdgeKey, Edge>()

/**
 * ★ [15] (5) — the temporal scrubber's track, maintained incrementally.
 *
 * See TimelineTracker: the scrubber used to derive its [min, max] by scanning every
 * node/edge/finding on each structural change, which put an O(N+E) walk on the push
 * path. Every timestamped insert below feeds this instead; every removal marks it
 * for one lazy rebuild.
 */
const timeline = new TimelineTracker()

/** Renderer-facing accessors. Not reactive by design ([7-D]). */
export const graphData = {
  nodes,
  edges,
  getNode: (id: string): Node | undefined => nodes.get(id),
  getEdge: (key: EdgeKey): Edge | undefined => edges.get(key),
  nodeCount: (): number => nodes.size,
  edgeCount: (): number => edges.size,
  reset(): void {
    nodes.clear()
    edges.clear()
    timeline.reset()
  },
}

/**
 * The scrubber's [min, max] created_at, or null for an empty graph ([2-B]/M3).
 *
 * O(1) on the streaming path — the cost is paid one item at a time as data arrives,
 * not re-derived per push. Only a removal makes the next call rescan.
 */
export function timelineBoundsNow(findings: Map<string, Finding>): TimelineBounds | null {
  return timeline.bounds(nodes, edges, findings)
}

/** [5-D] one applied AI style — the resolved ids and the allowlisted values. */
export interface AiStyle {
  ids: string[]
  style: StyleValue
}

/** [5-D] a screen-space AI note. */
export interface Annotation {
  x: number
  y: number
  text: string
}

/** [5-A] patch — set merges (properties merge key-wise), remove drops property keys. */
function applyPatch<T extends { properties: Record<string, unknown> }>(
  target: T,
  patch: Patch,
): T {
  const next: T = { ...target, properties: { ...target.properties } }
  for (const [name, value] of Object.entries(patch.set ?? {})) {
    if (name === 'properties') {
      next.properties = { ...next.properties, ...(value as Record<string, unknown>) }
    } else {
      ;(next as Record<string, unknown>)[name] = value
    }
  }
  for (const key of patch.remove ?? []) {
    delete next.properties[key]
  }
  return next
}

// --- store ([9-C]) ---

/** Shape of GET /graph.json ([8-C]); declared here to avoid a cycle with ws/client. */
export interface GraphSnapshotLike {
  seq: number
  nodes: Node[]
  edges: Edge[]
  findings: Finding[]
  layers?: string[]
  focus?: string | null
  active_filter?: string | null
}

export interface GraphState {
  /** [9-B] ConnectionBar shows totals; components read counts, not bodies ([7-D]). */
  nodeCount: number
  edgeCount: number
  findings: Map<string, Finding>
  layers: Map<string, LayerInfo>
  focus: string | null
  /** [5-C] shared filter: expression, the set it matched, and any server error. */
  filter: { expression: string; visibleIds: Set<string>; error: string | null }
  /** [5-D] AI's pending filter suggestion (a banner), or null. */
  suggestion: { expression: string; reason: string } | null
  /** [5-D] the layout the AI last asked the detail view to use, or null. */
  activeLayout: string | null
  /** [5-D] AI visual styles by id, and screen-space notes. Independent of the
   * filter/anchor overlays; TTL expiry removes them. */
  aiStyles: Map<string, AiStyle>
  annotations: Map<string, Annotation>
  viewMode: ViewMode
  zoom: number
  /**
   * [2-B]/M3 temporal scrubber — the created_at cutoff (epoch ms) the view is held
   * at, or null for live. A pure view/overlay state ([7-D] like the filter): it
   * never touches nodes/edges or structureSeq, so scrubbing stays off the
   * structural render path.
   */
  temporalCutoff: number | null
  /** Highest seq applied — the resync comparison point ([8-C]). */
  seq: number
  /**
   * Ticks only when the node/edge *set* changes ([7-A] overview 재시드 게이트).
   *
   * `seq` moves for everything on the wire — a finding recorded, a property
   * corrected, a filter set. The overview must not re-seed positions and settle
   * for those: [7-D] measured a position change at ~130ms plus a ~2.8s settle, so
   * gating the layout on `seq` means recording gold — the thing this tool exists
   * for — makes the graph lurch every time. Only add/remove changes the layout.
   *
   * Client-side and derived: it is not part of the [8-C] wire protocol and never
   * leaves the browser, it just remembers which flushes were structural.
   */
  structureSeq: number

  applyServerEvent: (event: WSEvent) => void
  /** [8-C] resync step 1: hold arriving events while the snapshot is fetched. */
  beginResync: () => void
  /** Release the hold without a snapshot (the fetch failed). */
  endResync: () => void
  /** [8-C] full-snapshot resync from GET /graph.json. */
  applySnapshot: (snapshot: GraphSnapshotLike) => void
  setFocus: (id: string | null) => void
  setFilter: (expression: string) => void
  /** [5-D] clear the AI suggestion banner (the human applied or dismissed it). */
  dismissSuggestion: () => void
  /** [5-D] TTL expiry / clear_style remove an AI style or annotation by id. */
  removeStyle: (styleId: string) => void
  removeAnnotation: (annotationId: string) => void
  toggleLayer: (layer: string) => void
  setViewMode: (mode: ViewMode) => void
  setZoom: (zoom: number) => void
  /** [M3c] Hold the view at a created_at cutoff (epoch ms), or null to go live. */
  setTemporalCutoff: (cutoffMs: number | null) => void
  /** [7-D] flush seam — normally driven by rAF; tests and resync call it directly. */
  flushNow: () => void
  reset: () => void
}

const initialState = {
  nodeCount: 0,
  edgeCount: 0,
  findings: new Map<string, Finding>(),
  layers: new Map<string, LayerInfo>(),
  focus: null,
  filter: { expression: '', visibleIds: new Set<string>(), error: null },
  suggestion: null as { expression: string; reason: string } | null,
  activeLayout: null as string | null,
  aiStyles: new Map<string, AiStyle>(),
  annotations: new Map<string, Annotation>(),
  viewMode: 'overview' as ViewMode,
  zoom: 1,
  temporalCutoff: null as number | null,
  seq: 0,
  structureSeq: 0,
}

let queue: WSEvent[] = []
let scheduled = false
/**
 * True while a resync snapshot is in flight ([8-C] M1: "WS 먼저 연결 (이벤트
 * 버퍼링) → seq 태그된 /graph.json fetch → snapshot seq 이하 버퍼 폐기 → 나머지 적용").
 *
 * The buffering is the whole point. Applying events while the fetch is running
 * means the snapshot — taken *before* them — lands afterwards and replaces the
 * graph, silently undoing everything that arrived in between.
 */
let suspended = false

/**
 * [7-D]/M3b — the structural delta a flush produced, so the overview maintains its
 * cosmos arrays in O(delta) instead of rebuilding O(N) per push. Accumulated across
 * flushes and drained by `consumeGraphDelta`, so a render that coalesces two flushes
 * still sees both. Net-tracked: a node added then deleted before it is drained never
 * reaches the arrays. A resync/clear sets `rebuild` — the arrays start over.
 */
export interface GraphDelta {
  addedNodes: string[]
  removedNodes: string[]
  addedEdges: EdgeKey[]
  removedEdges: EdgeKey[]
  /** Existing nodes whose data changed (re-push / update) — recolour/resize in place. */
  touchedNodes: string[]
  /** A wholesale replacement happened (resync / clear) — rebuild from scratch. */
  rebuild: boolean
}

let dAddedNodes = new Set<string>()
let dRemovedNodes = new Set<string>()
let dAddedEdges = new Set<EdgeKey>()
let dRemovedEdges = new Set<EdgeKey>()
let dTouchedNodes = new Set<string>()
let dRebuild = false

function noteNodeAdd(id: string, isNew: boolean): void {
  if (isNew) {
    dAddedNodes.add(id)
    dRemovedNodes.delete(id)
    dTouchedNodes.delete(id)
  } else if (!dAddedNodes.has(id)) {
    dTouchedNodes.add(id) // a re-push (e.g. placeholder resolved) — recolour, keep place
  }
}
function noteNodeUpdate(id: string): void {
  if (!dAddedNodes.has(id)) dTouchedNodes.add(id)
}
function noteNodeDelete(id: string): void {
  if (dAddedNodes.has(id)) dAddedNodes.delete(id)
  else dRemovedNodes.add(id)
  dTouchedNodes.delete(id)
}
function noteEdgeAdd(key: EdgeKey, isNew: boolean): void {
  if (isNew) {
    dAddedEdges.add(key)
    dRemovedEdges.delete(key)
  }
}
function noteEdgeDelete(key: EdgeKey): void {
  if (dAddedEdges.has(key)) dAddedEdges.delete(key)
  else dRemovedEdges.add(key)
}
function markRebuild(): void {
  dRebuild = true
  dAddedNodes.clear()
  dRemovedNodes.clear()
  dAddedEdges.clear()
  dRemovedEdges.clear()
  dTouchedNodes.clear()
}

/** Drain the accumulated structural delta ([7-D]/M3b). The overview calls this once
 * per data effect run; whatever it does not consume this frame stays for the next. */
export function consumeGraphDelta(): GraphDelta {
  const delta: GraphDelta = {
    addedNodes: [...dAddedNodes],
    removedNodes: [...dRemovedNodes],
    addedEdges: [...dAddedEdges],
    removedEdges: [...dRemovedEdges],
    touchedNodes: [...dTouchedNodes],
    rebuild: dRebuild,
  }
  dAddedNodes = new Set()
  dRemovedNodes = new Set()
  dAddedEdges = new Set()
  dRemovedEdges = new Set()
  dTouchedNodes = new Set()
  dRebuild = false
  return delta
}

function schedule(): void {
  if (scheduled || suspended) return
  scheduled = true
  const raf =
    typeof requestAnimationFrame === 'function'
      ? requestAnimationFrame
      : (cb: FrameRequestCallback) => setTimeout(() => cb(0), 50) as unknown as number
  raf(() => {
    scheduled = false
    useGraphStore.getState().flushNow()
  })
}

/** [5-D] TTL — schedule an overlay's own removal. setTimeout so tests can advance it. */
function scheduleExpiry(fn: () => void, ttlSeconds: number): void {
  setTimeout(fn, ttlSeconds * 1000)
}

/** Track a layer the moment it is first seen, so LayerToggleList can list it ([9-B]). */
function noteLayer(layers: Map<string, LayerInfo>, layer: string | null): void {
  if (layer && !layers.has(layer)) {
    layers.set(layer, { visible: true, color: null })
  }
}

export const useGraphStore = create<GraphState>((set, get) => ({
  ...initialState,

  applyServerEvent: (event) => {
    queue.push(event)
    schedule()
  },

  beginResync: () => {
    suspended = true
  },

  endResync: () => {
    suspended = false
    if (queue.length > 0) schedule()
  },

  applySnapshot: (snapshot) => {
    // [8-C] M1 resync: the snapshot *is* the state at its seq, so it replaces
    // rather than merges. Queued events at or below that seq are already baked in
    // and get dropped; anything newer stays queued and applies on top.
    graphData.reset()
    const layers = new Map<string, LayerInfo>()
    for (const node of snapshot.nodes) {
      nodes.set(node.id, node)
      noteLayer(layers, node.layer)
      timeline.observe(node.created_at)
    }
    for (const edge of snapshot.edges) {
      edges.set(edgeKeyOf(edge), edge)
      noteLayer(layers, edge.layer)
      timeline.observe(edge.created_at)
    }
    const findings = new Map<string, Finding>()
    for (const finding of snapshot.findings) {
      findings.set(finding.finding_id, finding)
      noteLayer(layers, finding.layer)
      timeline.observe(finding.created_at)
    }
    for (const layer of snapshot.layers ?? []) noteLayer(layers, layer)

    queue = queue.filter((event) => event.seq > snapshot.seq)
    // A resync replaces every node/edge, so the overview must rebuild, not diff.
    markRebuild()
    set({
      nodeCount: nodes.size,
      edgeCount: edges.size,
      findings,
      layers,
      focus: snapshot.focus ?? null,
      filter: {
        expression: snapshot.active_filter ?? '',
        visibleIds: get().filter.visibleIds,
        error: null,
      },
      seq: snapshot.seq,
      // A resync replaces every node and edge, so the layout must re-seed.
      structureSeq: get().structureSeq + 1,
    })
    // Buffered events newer than the snapshot now apply on top of it.
    suspended = false
    if (queue.length > 0) schedule()
  },

  flushNow: () => {
    // Nothing applies while a resync is pending, whichever path calls in — the
    // snapshot has to land first or it will overwrite what we just applied.
    if (suspended) return
    if (queue.length === 0) return
    const batch = queue
    queue = []

    // Mutate the off-React maps first, then write the store once ([7-D]).
    const findings = new Map(get().findings)
    const layers = new Map(get().layers)
    const aiStyles = new Map(get().aiStyles)
    const annotations = new Map(get().annotations)
    let { focus, filter, seq, structureSeq, suggestion, activeLayout } = get()
    /**
     * Set at the add/delete sites rather than derived from a count.
     * nodeCount+edgeCount deltas would miss a flush that deletes one node and
     * adds another: the totals come out identical while the set has changed and
     * the new node still needs a seed.
     */
    let structural = false

    for (const event of batch) {
      seq = Math.max(seq, event.seq)
      switch (event.op) {
        case 'graph.batch': {
          const d = event.data
          for (const node of d.nodes_added) {
            const isNew = !nodes.has(node.id)
            nodes.set(node.id, node)
            noteLayer(layers, node.layer)
            noteNodeAdd(node.id, isNew)
            timeline.observe(node.created_at)
          }
          for (const upd of d.nodes_updated) {
            applyNodeUpdate(upd, layers)
            noteNodeUpdate(upd.id)
          }
          for (const del of d.nodes_deleted) {
            if (nodes.delete(del.id)) {
              noteNodeDelete(del.id)
              timeline.invalidate()
            }
          }
          for (const edge of d.edges_added) {
            const key = edgeKeyOf(edge)
            const isNew = !edges.has(key)
            edges.set(key, edge)
            noteLayer(layers, edge.layer)
            noteEdgeAdd(key, isNew)
            timeline.observe(edge.created_at)
          }
          for (const upd of d.edges_updated) applyEdgeUpdate(upd, layers)
          for (const del of d.edges_deleted) {
            const key = edgeKeyOf(del)
            if (edges.delete(key)) {
              noteEdgeDelete(key)
              timeline.invalidate()
            }
          }
          // *_updated is deliberately absent: an update changes what a node says,
          // never whether it exists, so it is an overlay concern, not a layout one.
          if (
            d.nodes_added.length > 0 ||
            d.nodes_deleted.length > 0 ||
            d.edges_added.length > 0 ||
            d.edges_deleted.length > 0
          ) {
            structural = true
          }
          break
        }
        case 'node.add': {
          const isNew = !nodes.has(event.data.id)
          nodes.set(event.data.id, event.data)
          noteLayer(layers, event.data.layer)
          noteNodeAdd(event.data.id, isNew)
          timeline.observe(event.data.created_at)
          structural = true
          break
        }
        case 'node.update':
          applyNodeUpdate(event.data, layers)
          noteNodeUpdate(event.data.id)
          break
        case 'node.delete':
          if (nodes.delete(event.data.id)) {
            noteNodeDelete(event.data.id)
            timeline.invalidate()
          }
          structural = true
          break
        case 'edge.add': {
          const key = edgeKeyOf(event.data)
          const isNew = !edges.has(key)
          edges.set(key, event.data)
          noteLayer(layers, event.data.layer)
          noteEdgeAdd(key, isNew)
          timeline.observe(event.data.created_at)
          structural = true
          break
        }
        case 'edge.update':
          applyEdgeUpdate(event.data, layers)
          break
        case 'edge.delete': {
          const key = edgeKeyOf(event.data)
          if (edges.delete(key)) {
            noteEdgeDelete(key)
            timeline.invalidate()
          }
          structural = true
          break
        }
        case 'finding.add':
          findings.set(event.data.finding_id, event.data)
          noteLayer(layers, event.data.layer)
          timeline.observe(event.data.created_at)
          break
        case 'finding.update': {
          const existing = findings.get(event.data.finding_id)
          if (existing) {
            findings.set(event.data.finding_id, {
              ...existing,
              ...(event.data.patch.set as Partial<Finding>),
            })
          }
          break
        }
        case 'finding.delete':
          if (findings.delete(event.data.finding_id)) timeline.invalidate()
          break
        case 'filter.set':
          // [5-C] An error reply means the server rejected this filter and left
          // the shared view unchanged ([8-C]): keep the current filter, surface
          // the message. Otherwise apply the new expression + visible set.
          if (event.data.error) {
            filter = { ...filter, error: event.data.error }
          } else {
            filter = {
              expression: event.data.expression,
              visibleIds: new Set(event.data.visible_ids),
              error: null,
            }
          }
          break
        case 'focus.set':
          focus = event.data.id
          break
        case 'layer.toggle': {
          const info = layers.get(event.data.layer) ?? { visible: true, color: null }
          layers.set(event.data.layer, { ...info, visible: event.data.visible })
          break
        }
        case 'style.apply': {
          // [5-D] AI style overlay ([7-D]: a colour overlay, not a topology
          // change — structureSeq stays put, so the overview recolours on the
          // cheap path). TTL>0 schedules its own removal.
          const { style_id, ids, style, ttl } = event.data
          aiStyles.set(style_id, { ids, style })
          if (ttl > 0) scheduleExpiry(() => useGraphStore.getState().removeStyle(style_id), ttl)
          break
        }
        case 'style.clear':
          if (event.data.style_id) aiStyles.delete(event.data.style_id)
          else aiStyles.clear()
          break
        case 'annotation.add': {
          const { annotation_id, x, y, text, ttl } = event.data
          annotations.set(annotation_id, { x, y, text })
          if (ttl > 0)
            scheduleExpiry(() => useGraphStore.getState().removeAnnotation(annotation_id), ttl)
          break
        }
        case 'clear':
          applyClear(event.data, layers)
          structural = true // nodes/edges removed ([5-A]); findings survive
          markRebuild() // a bulk removal — the overview rebuilds rather than diffing
          break
        case 'filter.suggest':
          // [5-D] AI proposes a filter; the human decides. Held as a banner, not
          // applied — applying is the human clicking [적용] (→ sendFilterSet).
          suggestion = { expression: event.data.expression, reason: event.data.reason }
          break
        case 'layout.set':
          // [5-D] AI asks the detail view for a layout ([7-B] enum).
          activeLayout = event.data.algorithm
          break
        default:
          // snapshot.load is a serve concern — ignored here rather than dropped
          // on the floor by a throw, so an unknown op can never break the stream.
          break
      }
    }

    set({
      nodeCount: nodes.size,
      edgeCount: edges.size,
      findings,
      layers,
      focus,
      filter,
      seq,
      structureSeq: structural ? structureSeq + 1 : structureSeq,
      suggestion,
      activeLayout,
      aiStyles,
      annotations,
    })
  },

  setFocus: (id) => set({ focus: id }),

  dismissSuggestion: () => set({ suggestion: null }),

  removeStyle: (styleId) => {
    const aiStyles = new Map(get().aiStyles)
    if (aiStyles.delete(styleId)) set({ aiStyles })
  },

  removeAnnotation: (annotationId) => {
    const annotations = new Map(get().annotations)
    if (annotations.delete(annotationId)) set({ annotations })
  },

  setFilter: (expression) =>
    // Local echo of what the human typed; the server's filter.set broadcast is
    // authoritative for visibleIds. Clears any stale error on a fresh edit.
    set({ filter: { expression, visibleIds: get().filter.visibleIds, error: null } }),

  toggleLayer: (layer) => {
    const layers = new Map(get().layers)
    const info = layers.get(layer) ?? { visible: true, color: null }
    layers.set(layer, { ...info, visible: !info.visible })
    set({ layers })
  },

  setViewMode: (viewMode) => set({ viewMode }),

  setTemporalCutoff: (temporalCutoff) => set({ temporalCutoff }),
  setZoom: (zoom) => set({ zoom }),

  reset: () => {
    queue = []
    scheduled = false
    suspended = false
    graphData.reset()
    markRebuild() // everything cleared — the overview rebuilds from empty
    set({
      ...initialState,
      findings: new Map(),
      layers: new Map(),
      filter: { expression: '', visibleIds: new Set(), error: null },
      aiStyles: new Map(),
      annotations: new Map(),
    })
  },
}))

function applyNodeUpdate(update: NodeUpdateData, layers: Map<string, LayerInfo>): void {
  const existing = nodes.get(update.id)
  if (!existing) return
  const next = applyPatch(existing, update.patch)
  nodes.set(update.id, next)
  noteLayer(layers, next.layer)
}

function applyEdgeUpdate(update: EdgeUpdateData, layers: Map<string, LayerInfo>): void {
  const key = edgeKey(update.source, update.target, update.relation, update.key)
  const existing = edges.get(key)
  if (!existing) return
  const next = applyPatch(existing, update.patch)
  edges.set(key, next)
  noteLayer(layers, next.layer)
}

/**
 * [8-C] clear — a layer when named ([5-A] clear_layer), otherwise everything.
 *
 * findings are never dropped: clear_* wipes the graph, not the session's gold
 * ([5-A] clear_all keeps snapshots; [23-B] findings are a first-class collection,
 * not graph topology). A finding anchored to a removed node only loses that
 * anchor — the server sends the matching finding.update, and get_finding surfaces
 * the rest as `missing`.
 *
 * The server cascades: an edge touching a removed node goes too, whatever layer
 * owns it, because [4] forbids dangling edges. Mirrored here by dropping edges
 * whose endpoints are gone — otherwise the client would keep edges the server no
 * longer has.
 */
function applyClear(data: ClearData, layers: Map<string, LayerInfo>): void {
  // Either shape removes graph data, so the scrubber's track may have narrowed —
  // findings survive a clear ([5-A]) and can still hold the bounds open.
  timeline.invalidate()
  if (!data.layer) {
    nodes.clear()
    edges.clear()
    layers.clear()
    return
  }
  for (const [id, node] of [...nodes]) if (node.layer === data.layer) nodes.delete(id)
  for (const [key, edge] of [...edges]) {
    const orphaned = !nodes.has(edge.source) || !nodes.has(edge.target)
    if (edge.layer === data.layer || orphaned) edges.delete(key)
  }
  layers.delete(data.layer)
}

export type { Edge, EdgeIdentityData, Finding, Node }
