/**
 * Graph Core data → cosmos.gl typed arrays ([7-A], [7-D]).
 *
 * cosmos.gl speaks flat Float32Arrays and refers to points by index, never by id.
 * That suits [7-D] exactly: the bulk data already lives outside React, and this
 * converts it into the shape the GPU wants without any of it passing through the
 * store.
 *
 * Kept as pure functions so the parts with real logic — degree, colour, the
 * index↔id mapping — are testable without a WebGL context.
 */

import type { Edge, Node } from '../types'
import { PLACEHOLDER_RGB, typeColor } from './palette'

export const MIN_POINT_SIZE = 4
export const MAX_POINT_SIZE = 24

export interface GraphArrays {
  /** [x, y] per point. */
  pointPositions: Float32Array
  /** [r, g, b, a] per point, 0..1. */
  pointColors: Float32Array
  pointSizes: Float32Array
  /** [sourceIndex, targetIndex] per link. */
  links: Float32Array
  /** Position in the arrays for a node id. */
  indexById: Map<string, number>
  ids: string[]
}

/** cosmos refers to points by index; the click handler needs the id back. */
export function idAtIndex(arrays: GraphArrays, index: number): string | undefined {
  return arrays.ids[index]
}

export function degreeOf(edges: Iterable<Edge>): Map<string, number> {
  const degree = new Map<string, number>()
  for (const edge of edges) {
    degree.set(edge.source, (degree.get(edge.source) ?? 0) + 1)
    degree.set(edge.target, (degree.get(edge.target) ?? 0) + 1)
  }
  return degree
}

/** [7-A] 노드 크기: degree 기반 (log scale) — hubs read as hubs without swamping. */
export function sizeForDegree(degree: number, maxDegree: number): number {
  if (maxDegree <= 0) return MIN_POINT_SIZE
  const scaled = Math.log1p(degree) / Math.log1p(maxDegree)
  return MIN_POINT_SIZE + scaled * (MAX_POINT_SIZE - MIN_POINT_SIZE)
}

/** [7-A] 노드 색: type 별 자동 팔레트 (또는 layer 색). */
export function colorForNode(node: Node, colorBy: 'type' | 'layer'): [number, number, number] {
  if (node.properties?.placeholder === true) return PLACEHOLDER_RGB
  if (colorBy === 'layer') return typeColor(node.layer ?? 'unlayered')
  return typeColor(node.type)
}

/**
 * How much of the space the initial seed cloud spans, as a fraction.
 *
 * Small on purpose. Seeds are only a starting point — the force simulation does
 * the layout. Scattering them across the whole space instead makes the initial
 * fitView frame a cloud that is mostly empty, so the camera starts zoomed far
 * out and [7-A]'s LOD suppresses every label until the user zooms in. Worse, how
 * far out depends on where ids happen to hash to, so whether a small graph shows
 * labels would vary with the node names.
 */
const SEED_SPREAD = 0.125

/** How far a neighbour-seeded node is jittered off its anchor, as a fraction. */
const NEIGHBOUR_JITTER = 0.01

/** Deterministic hash — same id always seeds to the same place. */
function hashOf(id: string): number {
  let hash = 2166136261
  for (let i = 0; i < id.length; i += 1) {
    hash ^= id.charCodeAt(i)
    hash = Math.imul(hash, 16777619) >>> 0
  }
  return hash
}

/** Deterministic seed position — used when a node has nowhere better to go. */
function seedPosition(id: string, spaceSize: number): [number, number] {
  const hash = hashOf(id)
  const centre = spaceSize / 2
  const extent = spaceSize * SEED_SPREAD
  const x = centre + (((hash & 0xffff) / 0xffff) - 0.5) * extent
  const y = centre + ((((hash >>> 16) & 0xffff) / 0xffff) - 0.5) * extent
  return [x, y]
}

/** Adjacency for seeding — a new node wants to land near what it connects to. */
function addAdjacency(adjacency: Map<string, string[]>, a: string, b: string): void {
  const list = adjacency.get(a)
  if (list) list.push(b)
  else adjacency.set(a, [b])
}

function neighboursOf(edges: Map<string, Edge>): Map<string, string[]> {
  const adjacency = new Map<string, string[]>()
  for (const edge of edges.values()) {
    addAdjacency(adjacency, edge.source, edge.target)
    addAdjacency(adjacency, edge.target, edge.source)
  }
  return adjacency
}

type ResolvePos = (id: string) => readonly [number, number] | undefined

/**
 * Where a node should appear ([7-D] A: 신규 노드는 연결 이웃 근처).
 *
 * Extracted so the full build and the incremental append seed by the *same* rule
 * — the invariant that lets one hand off to the other without teleporting. Order:
 * keep an existing position, else anchor to already-placed neighbours, else the
 * deterministic hash seed. ``resolve`` returns a live position for an id (this
 * pass's ``placed`` first, then the previous frame's positions).
 */
function computePosition(
  id: string,
  adjacency: Map<string, string[]>,
  resolve: ResolvePos,
  spaceSize: number,
): readonly [number, number] {
  const existing = resolve(id)
  if (existing && Number.isFinite(existing[0]) && Number.isFinite(existing[1])) return existing

  const anchors: Array<readonly [number, number]> = []
  for (const neighbour of adjacency.get(id) ?? []) {
    const at = resolve(neighbour)
    if (at && Number.isFinite(at[0]) && Number.isFinite(at[1])) anchors.push(at)
  }
  if (anchors.length === 0) return seedPosition(id, spaceSize)

  let x = 0
  let y = 0
  for (const [ax, ay] of anchors) {
    x += ax
    y += ay
  }
  x /= anchors.length
  y /= anchors.length
  const hash = hashOf(id)
  const spread = spaceSize * NEIGHBOUR_JITTER
  return [
    x + (((hash & 0xffff) / 0xffff) - 0.5) * spread,
    y + ((((hash >>> 16) & 0xffff) / 0xffff) - 0.5) * spread,
  ]
}

/** Write a node's [7-A] type/layer colour into a flat rgba buffer at point index i. */
function writeColorInto(colors: Float32Array, i: number, node: Node, colorBy: 'type' | 'layer'): void {
  const [r, g, b] = colorForNode(node, colorBy)
  colors[i * 4] = r / 255
  colors[i * 4 + 1] = g / 255
  colors[i * 4 + 2] = b / 255
  colors[i * 4 + 3] = 1
}

export interface BuildOptions {
  colorBy?: 'type' | 'layer'
  spaceSize?: number
  /**
   * Positions already on screen, by id.
   *
   * [7-D] 레이아웃 수명주기: during INGESTING the simulation is off, so a node
   * that is already placed must keep exactly where it is — re-seeding it would
   * teleport the graph out from under the reader on every push, and would throw
   * away the layout SETTLING computed. Only genuinely new nodes get a seed.
   */
  previous?: (id: string) => readonly [number, number] | undefined
}

export function buildGraphArrays(
  nodes: Map<string, Node>,
  edges: Map<string, Edge>,
  options: BuildOptions = {},
): GraphArrays {
  const colorBy = options.colorBy ?? 'type'
  const spaceSize = options.spaceSize ?? 4096
  const previous = options.previous

  const ids = [...nodes.keys()]
  const indexById = new Map<string, number>()
  ids.forEach((id, i) => indexById.set(id, i))

  const degree = degreeOf(edges.values())
  const maxDegree = Math.max(0, ...degree.values())
  const adjacency = neighboursOf(edges)

  const pointPositions = new Float32Array(ids.length * 2)
  const pointColors = new Float32Array(ids.length * 4)
  const pointSizes = new Float32Array(ids.length)

  // Placed so far this pass, so a new node can anchor to a neighbour that is
  // itself new — a batch arriving together still clusters instead of scattering.
  const placed = new Map<string, readonly [number, number]>()
  const resolve: ResolvePos = (id) => placed.get(id) ?? previous?.(id)

  ids.forEach((id, i) => {
    const node = nodes.get(id)!
    const at = computePosition(id, adjacency, resolve, spaceSize)
    placed.set(id, at)
    pointPositions[i * 2] = at[0]
    pointPositions[i * 2 + 1] = at[1]
    writeColorInto(pointColors, i, node, colorBy)
    pointSizes[i] = sizeForDegree(degree.get(id) ?? 0, maxDegree)
  })

  // An edge to a node we do not hold is dropped rather than drawn to index -1:
  // the server cascades so this should not happen, but a half-applied batch
  // must not crash the renderer.
  const linkPairs: number[] = []
  for (const edge of edges.values()) {
    const source = indexById.get(edge.source)
    const target = indexById.get(edge.target)
    if (source === undefined || target === undefined) continue
    linkPairs.push(source, target)
  }

  return {
    pointPositions,
    pointColors,
    pointSizes,
    links: new Float32Array(linkPairs),
    indexById,
    ids,
  }
}

/** The structural change a flush produced — the input to O(delta) maintenance. */
export interface StructuralDelta {
  /** Genuinely new node ids (append a point). */
  addedNodes: string[]
  /** New edge keys (append a link, degree++ both endpoints). */
  addedEdges: string[]
  /** Existing nodes re-pushed / updated — recolour/resize in place, keep position. */
  touchedNodes: string[]
}

const DEFAULT_SPACE_SIZE = 4096

/**
 * O(delta) incremental cosmos arrays ([7-D] "incremental update — no full reset").
 *
 * ``buildGraphArrays`` rebuilds every point on every structural change, which is
 * O(N) per push and 120ms at 100K (M3a). This maintains the same arrays in place:
 * an add appends the new node/link and only re-sizes the nodes whose degree
 * actually changed, so a single push is O(delta), not O(N). The result is
 * byte-for-byte what ``buildGraphArrays`` would produce for the same final state
 * (shared ``computePosition`` / colour / size), so a fallback ``rebuild`` on a
 * removal never teleports anything.
 *
 * The one genuinely O(N) case is preserved because it is *correct*: node size is
 * ``sizeForDegree(degree, maxDegree)`` normalised by the **global** maxDegree, so
 * an edge that raises maxDegree does shift every node's size. That is rare (a new
 * record-degree hub); the common edge only resizes its two endpoints.
 *
 * cosmos still receives the whole array each flush — that is a ~5ms memcpy (M3a),
 * not the cost. Only the JS-side rebuild is incrementalised.
 */
export class IncrementalCosmosArrays {
  private ids: string[] = []
  private readonly indexById = new Map<string, number>()
  private readonly degree = new Map<string, number>()
  private maxDegree = 0
  private readonly adjacency = new Map<string, string[]>()
  private pos: Float32Array = new Float32Array(0)
  private col: Float32Array = new Float32Array(0)
  private siz: Float32Array = new Float32Array(0)
  private links: Float32Array = new Float32Array(0)
  private nodeCap = 0
  private linkCap = 0
  private linkCount = 0
  private readonly colorBy: 'type' | 'layer'
  private readonly spaceSize: number

  constructor(options: { colorBy?: 'type' | 'layer'; spaceSize?: number } = {}) {
    this.colorBy = options.colorBy ?? 'type'
    this.spaceSize = options.spaceSize ?? DEFAULT_SPACE_SIZE
  }

  get count(): number {
    return this.ids.length
  }

  private ensureNodeCapacity(needed: number): void {
    if (needed <= this.nodeCap) return
    const cap = Math.max(needed, this.nodeCap === 0 ? 1024 : this.nodeCap * 2)
    const pos = new Float32Array(cap * 2)
    pos.set(this.pos.subarray(0, this.count * 2))
    const col = new Float32Array(cap * 4)
    col.set(this.col.subarray(0, this.count * 4))
    const siz = new Float32Array(cap)
    siz.set(this.siz.subarray(0, this.count))
    this.pos = pos
    this.col = col
    this.siz = siz
    this.nodeCap = cap
  }

  private ensureLinkCapacity(needed: number): void {
    if (needed <= this.linkCap) return
    const cap = Math.max(needed, this.linkCap === 0 ? 1024 : this.linkCap * 2)
    const links = new Float32Array(cap * 2)
    links.set(this.links.subarray(0, this.linkCount * 2))
    this.links = links
    this.linkCap = cap
  }

  /** Full rebuild — the fallback for removals/resync, and the first build. O(N). */
  rebuild(
    nodes: Map<string, Node>,
    edges: Map<string, Edge>,
    previous?: ResolvePos,
  ): GraphArrays {
    const built = buildGraphArrays(nodes, edges, {
      colorBy: this.colorBy,
      spaceSize: this.spaceSize,
      previous,
    })
    this.ids = [...built.ids]
    this.indexById.clear()
    built.indexById.forEach((v, k) => this.indexById.set(k, v))
    this.degree.clear()
    for (const [k, v] of degreeOf(edges.values())) this.degree.set(k, v)
    this.maxDegree = Math.max(0, ...this.degree.values())
    this.adjacency.clear()
    for (const [k, v] of neighboursOf(edges)) this.adjacency.set(k, v)
    // Adopt the freshly-built arrays as the growable buffers (exact size; the next
    // add grows them). subarray views handed out by snapshot() stay valid until then.
    this.pos = built.pointPositions
    this.col = built.pointColors
    this.siz = built.pointSizes
    this.links = built.links
    this.nodeCap = built.ids.length
    this.linkCap = built.links.length / 2
    this.linkCount = built.links.length / 2
    return this.snapshot()
  }

  /**
   * Apply an add-only delta in O(delta). ``previous`` resolves an existing node's
   * position (OverviewCanvas's authoritative positions map) so a new node seeds
   * beside neighbours that are already placed — identical to the full build.
   */
  applyAdd(
    delta: StructuralDelta,
    nodes: Map<string, Node>,
    edges: Map<string, Edge>,
    previous?: ResolvePos,
  ): GraphArrays {
    // 1) Degree + adjacency from the new edges first, so a new node can seed beside
    //    a neighbour it only just connected to. Track which nodes need a resize.
    const degreeChanged = new Set<string>()
    let maxGrew = false
    const addedEdgeObjs: Edge[] = []
    for (const key of delta.addedEdges) {
      const edge = edges.get(key)
      if (!edge) continue
      addedEdgeObjs.push(edge)
      addAdjacency(this.adjacency, edge.source, edge.target)
      addAdjacency(this.adjacency, edge.target, edge.source)
      const ds = (this.degree.get(edge.source) ?? 0) + 1
      const dt = (this.degree.get(edge.target) ?? 0) + 1
      this.degree.set(edge.source, ds)
      this.degree.set(edge.target, dt)
      if (ds > this.maxDegree) {
        this.maxDegree = ds
        maxGrew = true
      }
      if (dt > this.maxDegree) {
        this.maxDegree = dt
        maxGrew = true
      }
      degreeChanged.add(edge.source)
      degreeChanged.add(edge.target)
    }

    // 2) Append new nodes (position beside neighbours / seed, colour, size).
    this.ensureNodeCapacity(this.count + delta.addedNodes.length)
    const placed = new Map<string, readonly [number, number]>()
    const resolve: ResolvePos = (id) => placed.get(id) ?? previous?.(id)
    for (const id of delta.addedNodes) {
      const node = nodes.get(id)
      if (!node || this.indexById.has(id)) continue // guard against a double-add
      const i = this.ids.length
      this.ids.push(id)
      this.indexById.set(id, i)
      const at = computePosition(id, this.adjacency, resolve, this.spaceSize)
      placed.set(id, at)
      this.pos[i * 2] = at[0]
      this.pos[i * 2 + 1] = at[1]
      writeColorInto(this.col, i, node, this.colorBy)
      this.siz[i] = sizeForDegree(this.degree.get(id) ?? 0, this.maxDegree)
    }

    // 3) Append new links (both endpoints now indexed; drop a dangling one).
    this.ensureLinkCapacity(this.linkCount + addedEdgeObjs.length)
    for (const edge of addedEdgeObjs) {
      const s = this.indexById.get(edge.source)
      const t = this.indexById.get(edge.target)
      if (s === undefined || t === undefined) continue
      this.links[this.linkCount * 2] = s
      this.links[this.linkCount * 2 + 1] = t
      this.linkCount += 1
    }

    // 4) Resize. maxGrew → every node (the denominator moved); else only the
    //    degree-changed endpoints. New nodes were already sized in step 2.
    if (maxGrew) {
      for (let i = 0; i < this.count; i += 1) {
        this.siz[i] = sizeForDegree(this.degree.get(this.ids[i]) ?? 0, this.maxDegree)
      }
    } else {
      for (const id of degreeChanged) {
        const i = this.indexById.get(id)
        if (i !== undefined) this.siz[i] = sizeForDegree(this.degree.get(id) ?? 0, this.maxDegree)
      }
    }

    // 5) Re-pushed / updated existing nodes: recolour + resize in place, keep position.
    for (const id of delta.touchedNodes) {
      const i = this.indexById.get(id)
      const node = nodes.get(id)
      if (i === undefined || !node) continue
      writeColorInto(this.col, i, node, this.colorBy)
      this.siz[i] = sizeForDegree(this.degree.get(id) ?? 0, this.maxDegree)
    }

    return this.snapshot()
  }

  /** Recolour/resize the given existing nodes in place (overlay path). O(delta). */
  touch(ids: Iterable<string>, nodes: Map<string, Node>): GraphArrays {
    for (const id of ids) {
      const i = this.indexById.get(id)
      const node = nodes.get(id)
      if (i === undefined || !node) continue
      writeColorInto(this.col, i, node, this.colorBy)
      this.siz[i] = sizeForDegree(this.degree.get(id) ?? 0, this.maxDegree)
    }
    return this.snapshot()
  }

  /** Current arrays as exact-length views (cosmos copies them; capacity is hidden). */
  snapshot(): GraphArrays {
    return {
      pointPositions: this.pos.subarray(0, this.count * 2),
      pointColors: this.col.subarray(0, this.count * 4),
      pointSizes: this.siz.subarray(0, this.count),
      links: this.links.subarray(0, this.linkCount * 2),
      indexById: this.indexById,
      ids: this.ids,
    }
  }
}
