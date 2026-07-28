/**
 * Temporal scrubber ([2-B]/M3, 계획서 TASK 11) — a created_at overlay.
 *
 * The graph's growth *is already timestamped*: every Node/Edge/Finding carries a
 * server ``created_at``. So "the graph at time T" is a pure visibility overlay —
 * show what was created at or before T, fade what came after — reusing the same
 * ``dim: {visibleIds}`` path the filter already drives. No event log, no
 * reconstruction, no structural change: scrubbing costs a filter-dim, so it stays
 * off the [7-D] structural render path (M3c decision A, coordinator-approved).
 *
 * ★ Limitation (by design, documented in the UI): this reveals the *current* graph
 * in creation order. A node deleted before now is not in the graph, so it cannot
 * reappear at a past T; an updated node shows its current value, not its value at
 * T. That is enough for the "how the graph grew" narrative ([23]); a faithful
 * time-travel replay (deletes, past values) is the follow-up (M3c option B).
 */

import type { Edge, Finding, Node } from '../types'

/** created_at (ISO) → epoch ms; an unparseable stamp sorts to the epoch start. */
export function toMs(createdAt: string): number {
  const t = Date.parse(createdAt)
  return Number.isNaN(t) ? 0 : t
}

export interface TimelineBounds {
  /** Earliest created_at in the graph (epoch ms). */
  min: number
  /** Latest created_at in the graph (epoch ms). */
  max: number
}

/**
 * The [min, max] created_at across every node/edge/finding (epoch ms), or null for
 * an empty graph. This is the scrubber's track; the slider maps [0,1] onto it.
 */
export function timelineBounds(
  nodes: Map<string, Node>,
  edges: Map<string, Edge>,
  findings: Map<string, Finding>,
): TimelineBounds | null {
  let min = Number.POSITIVE_INFINITY
  let max = Number.NEGATIVE_INFINITY
  const scan = (createdAt: string) => {
    const t = toMs(createdAt)
    if (t < min) min = t
    if (t > max) max = t
  }
  for (const n of nodes.values()) scan(n.created_at)
  for (const e of edges.values()) scan(e.created_at)
  for (const f of findings.values()) scan(f.created_at)
  if (min === Number.POSITIVE_INFINITY) return null
  return { min, max }
}

/**
 * Ids of nodes created at or before ``cutoffMs`` — the temporal-visible set. A null
 * cutoff means live (everything is visible), signalled by returning null so callers
 * can skip the overlay entirely.
 */
export function visibleNodeIds(
  nodes: Map<string, Node>,
  cutoffMs: number | null,
): Set<string> | null {
  if (cutoffMs === null) return null
  const visible = new Set<string>()
  for (const node of nodes.values()) {
    if (toMs(node.created_at) <= cutoffMs) visible.add(node.id)
  }
  return visible
}

/**
 * Ids of findings created at or before ``cutoffMs`` (for the findings panel). Null
 * cutoff → null (all visible).
 */
export function visibleFindingIds(
  findings: Map<string, Finding>,
  cutoffMs: number | null,
): Set<string> | null {
  if (cutoffMs === null) return null
  const visible = new Set<string>()
  for (const finding of findings.values()) {
    if (toMs(finding.created_at) <= cutoffMs) visible.add(finding.finding_id)
  }
  return visible
}

/**
 * The cosmos link array restricted to edges present at ``cutoffMs`` — an edge is
 * drawn only when it *and both its endpoints* are at or before T.
 *
 * ★ Endpoint consistency: an edge cannot predate its endpoints (the server creates
 * a node — or an auto-placeholder — no later than the edge that needs it), so an
 * edge ≤ T normally has both endpoints ≤ T. The explicit endpoint check makes that
 * invariant hold even for a half-applied stream, so a visible edge never dangles to
 * a faded point.
 */
export function visibleLinks(
  edges: Map<string, Edge>,
  indexById: Map<string, number>,
  visibleNodes: ReadonlySet<string>,
  cutoffMs: number,
): Float32Array {
  const pairs: number[] = []
  for (const edge of edges.values()) {
    if (toMs(edge.created_at) > cutoffMs) continue
    if (!visibleNodes.has(edge.source) || !visibleNodes.has(edge.target)) continue
    const s = indexById.get(edge.source)
    const t = indexById.get(edge.target)
    if (s === undefined || t === undefined) continue
    pairs.push(s, t)
  }
  return new Float32Array(pairs)
}
