/**
 * Wire types — the shapes Graph Core actually puts on the WebSocket.
 *
 * Field names are snake_case because the backend serializes its dataclasses with
 * `asdict()` ([4-A]/[4-B]/[23-B]). Store-side UI state uses camelCase ([9-C]);
 * the two are deliberately different vocabularies and must not be conflated.
 */

// --- domain objects ([4-A], [4-B], [23-B]) ---

export interface StyleHint {
  color?: string
  icon?: string
  size?: number
}

export interface PositionHint {
  x: number
  y: number
}

/** [4-A] */
export interface Node {
  id: string
  label: string
  type: string
  properties: Record<string, unknown>
  parent_id: string | null
  style_hint: StyleHint | null
  position_hint: PositionHint | null
  layer: string | null
  ttl: number
  tags: string[]
  created_at: string
  updated_at: string
  created_by: string | null
}

/** [4-B]. Identity is the (source, target, relation, key) 4-tuple. */
export interface Edge {
  source: string
  target: string
  relation: string
  key: string
  directed: boolean
  properties: Record<string, unknown>
  weight: number
  layer: string | null
  style_hint: StyleHint | null
  ttl: number
  tags: string[]
  created_at: string
  created_by: string | null
}

/** [23-B] gold nugget. */
export interface Finding {
  finding_id: string
  title: string
  body: string
  node_ids: string[]
  confidence: number
  evidence: string[]
  layer: string | null
  tags: string[]
  created_by: string | null
  created_at: string
  updated_at: string
  /**
   * [24-C] 이력/변경로그. Fields rather than reserved `properties` keys because a
   * Finding has no properties map ([23-B]) — the asymmetry with Node/Edge, whose
   * history lives at `properties._superseded`, is deliberate.
   */
  _superseded?: SupersededEntry[]
  _provenance?: ProvenanceEntry[]
}

/** [23-B] reserved `_citations` entry. */
export interface Citation {
  url: string
  title: string
  ts: string
}

/**
 * [24-C] reserved `_superseded` entry — a value that was valid and is now stale.
 *
 * `prev` holds only the fields the superseding patch changed, so its shape
 * follows whatever was archived. `by` is null until [10-A] carries the client
 * identity through to the server.
 */
export interface SupersededEntry {
  prev: Record<string, unknown>
  at: string
  by: string | null
}

/** [24-B] reserved `_provenance` entry — the wrong value itself is never kept. */
export interface ProvenanceEntry {
  action: string
  at: string
  by: string | null
}

/** [5-A] patch — set merges, remove deletes property keys. */
export interface Patch {
  set?: Record<string, unknown>
  remove?: string[]
}

// --- [8-C] Server → Client payloads ---

export interface NodeUpdateData {
  id: string
  patch: Patch
}

export interface NodeDeleteData {
  id: string
}

export interface EdgeIdentityData {
  source: string
  target: string
  relation: string
  key: string
}

export interface EdgeUpdateData extends EdgeIdentityData {
  patch: Patch
}

/** [8-C] — node/edge only; findings are not coalesced. */
export interface GraphBatchData {
  nodes_added: Node[]
  nodes_updated: NodeUpdateData[]
  nodes_deleted: NodeDeleteData[]
  edges_added: Edge[]
  edges_updated: EdgeUpdateData[]
  edges_deleted: EdgeIdentityData[]
}

export interface FindingUpdateData {
  finding_id: string
  patch: Patch
}

export interface FindingDeleteData {
  finding_id: string
}

export interface FilterSetData {
  expression: string
  visible_ids: string[]
  /**
   * [8-C] set by the server when the [6] filter was rejected (syntax / a safety
   * cap). Present → the shared filter is unchanged and this is a per-client
   * notice; null/absent → a normal applied filter.
   */
  error?: string | null
}

export interface FilterSuggestData {
  expression: string
  reason: string
}

export interface FocusSetData {
  id: string
}

export interface LayerToggleData {
  layer: string
  visible: boolean
}

export interface LayoutSetData {
  algorithm: string
  options: Record<string, unknown>
}

/** [5-D]/[11] the allowlisted style the server validated. */
export interface StyleValue {
  color?: string
  size?: number
  border?: { color?: string; width?: number }
}

export interface StyleApplyData {
  style_id: string
  /** Server-resolved target ids — the frontend has no filter DSL ([5-D]). */
  ids: string[]
  style: StyleValue
  ttl: number
}

export interface StyleClearData {
  style_id?: string | null
}

export interface AnnotationAddData {
  annotation_id: string
  x: number
  y: number
  text: string
  ttl: number
}

export interface SnapshotLoadData {
  snapshot_id: string
}

export interface ClearData {
  layer?: string | null
}

// --- [8-C] Server → Client envelopes. Every event carries seq. ---

interface Envelope<Op extends string, Data> {
  op: Op
  data: Data
  seq: number
}

export type NodeAddEvent = Envelope<'node.add', Node>
export type NodeUpdateEvent = Envelope<'node.update', NodeUpdateData>
export type NodeDeleteEvent = Envelope<'node.delete', NodeDeleteData>
export type EdgeAddEvent = Envelope<'edge.add', Edge>
export type EdgeUpdateEvent = Envelope<'edge.update', EdgeUpdateData>
export type EdgeDeleteEvent = Envelope<'edge.delete', EdgeIdentityData>
export type GraphBatchEvent = Envelope<'graph.batch', GraphBatchData>
export type FindingAddEvent = Envelope<'finding.add', Finding>
export type FindingUpdateEvent = Envelope<'finding.update', FindingUpdateData>
export type FindingDeleteEvent = Envelope<'finding.delete', FindingDeleteData>
export type FilterSetEvent = Envelope<'filter.set', FilterSetData>
export type FilterSuggestEvent = Envelope<'filter.suggest', FilterSuggestData>
export type FocusSetEvent = Envelope<'focus.set', FocusSetData>
export type LayerToggleEvent = Envelope<'layer.toggle', LayerToggleData>
export type LayoutSetEvent = Envelope<'layout.set', LayoutSetData>
export type StyleApplyEvent = Envelope<'style.apply', StyleApplyData>
export type StyleClearEvent = Envelope<'style.clear', StyleClearData>
export type AnnotationAddEvent = Envelope<'annotation.add', AnnotationAddData>
export type SnapshotLoadEvent = Envelope<'snapshot.load', SnapshotLoadData>
export type ClearEvent = Envelope<'clear', ClearData>
/** [8-C] heartbeat reply (KI-1) — liveness only, no payload. */
export type PongEvent = Envelope<'pong', Record<string, never>>

/**
 * The individual node and edge envelopes stay declared even though the hub
 * coalesces graph mutations into graph.batch: they are the batch's array
 * element shapes, and a client must not assume they can never arrive.
 */
export type WSEvent =
  | NodeAddEvent
  | NodeUpdateEvent
  | NodeDeleteEvent
  | EdgeAddEvent
  | EdgeUpdateEvent
  | EdgeDeleteEvent
  | GraphBatchEvent
  | FindingAddEvent
  | FindingUpdateEvent
  | FindingDeleteEvent
  | FilterSetEvent
  | FilterSuggestEvent
  | FocusSetEvent
  | LayerToggleEvent
  | LayoutSetEvent
  | StyleApplyEvent
  | StyleClearEvent
  | AnnotationAddEvent
  | SnapshotLoadEvent
  | ClearEvent
  | PongEvent

// --- [8-C] Client → Server ---

export type ViewMode = 'overview' | 'detail' | 'split'

export interface ClientViewUpdateData {
  mode: ViewMode
  zoom: number
  camera_pos: { x: number; y: number }
}

export type ClientEvent =
  | { op: 'focus.set'; data: { id: string } }
  | { op: 'filter.set'; data: { expression: string } }
  | { op: 'layer.toggle'; data: { layer: string } }
  | { op: 'layout.set'; data: { algorithm: string } }
  | { op: 'view.update'; data: ClientViewUpdateData }
  | { op: 'ping'; data: Record<string, never> } // [8-C] heartbeat (KI-1)
  | { op: 'undo'; data: Record<string, never> } // [M2e] reverse last graph mutation
  | { op: 'redo'; data: Record<string, never> } // [M2e] re-apply last undone mutation

// --- store-side ([9-C]) ---

/** [9-C] layers: Map<string, LayerInfo>. */
export interface LayerInfo {
  visible: boolean
  color: string | null
}

/** [4-B] edge identity as a Map key — [9-C]: JSON.stringify([s,t,r,k]). */
export type EdgeKey = string

export function edgeKey(
  source: string,
  target: string,
  relation: string,
  key: string,
): EdgeKey {
  return JSON.stringify([source, target, relation, key])
}

export function edgeKeyOf(edge: EdgeIdentityData): EdgeKey {
  return edgeKey(edge.source, edge.target, edge.relation, edge.key)
}
