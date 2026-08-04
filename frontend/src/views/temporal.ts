/**
 * Temporal scrubber ([2-B]/M3) — a created_at overlay.
 *
 * The graph's growth *is already timestamped*: every Node/Edge/Finding carries a
 * server ``created_at``. So "the graph at time T" is a pure visibility overlay —
 * show what was created at or before T, fade what came after — reusing the same
 * ``dim: {visibleIds}`` path the filter already drives. No event log, no
 * reconstruction, no structural change: scrubbing costs a filter-dim, so it stays
 * off the [7-D] structural render path (M3c decision A).
 *
 * ★ Limitation (by design, documented in the UI): this reveals the *current* graph
 * in creation order. A node deleted before now is not in the graph, so it cannot
 * reappear at a past T; an updated node shows its current value, not its value at
 * T. That is enough for the "how the graph grew" narrative ([23]); a faithful
 * time-travel replay (deletes, past values) is the follow-up (M3c option B).
 */

import type { Edge, Finding, Node } from '../types'

/**
 * ★ [15] 회귀 가드용 카운터 — created_at 을 몇 번 파싱했는가.
 *
 * M3c 회귀([15 개정])는 "push 1회당 N+E 번 파싱" 이었고, 그건 시간으로 재면 머신
 * 소음에 묻히지만 **세면** 정확히 드러난다(@100K 300,015회, 10K/30K 에서
 * 30,005/90,005 — 완전 선형). 그래서 테스트가 시간이 아니라 이 수를 단언한다:
 * push 1회당 파싱 수가 그래프 크기와 무관해야 한다.
 */
export const temporalCounters = { parses: 0 }

/** created_at (ISO) → epoch ms; an unparseable stamp sorts to the epoch start. */
export function toMs(createdAt: string): number {
  temporalCounters.parses += 1
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
 * ★ [15] (5) 회귀 방어 — 타임라인 경계를 **증분으로** 유지한다.
 *
 * M3c 는 이 경계를 위 `timelineBounds` 로, TemporalScrubber 의 useMemo(deps
 * [structureSeq]) 안에서 구했다. 즉 push 1회당 노드+엣지+finding 전부를 훑었고,
 * 그것도 React render phase 안이라 cosmos effect **앞에** 통째로 얹혔다
 * ([15 개정] 렌즈 B). 판정이 문자열 로그였던 시기와 겹쳐 3주간 아무도 몰랐다.
 *
 * created_at 은 불변이다 — 그래서 경계는 **추가로만 넓어진다**. 추가 1건당 O(1)
 * 비교면 충분하고, 그게 이 트래커다.
 *
 * 제거는 경계를 좁힐 수 있다. 정확성을 위해 제거가 있으면 `invalidate()` 로
 * 표시하고 **다음 읽기에서 한 번** 전체 스캔으로 다시 만든다. 낡은 캐시를 들고
 * 있지 않으므로 트랙은 항상 실제 데이터와 일치한다. 비용을 감당하는 근거는
 * 제거가 드물다는 것([8-E] "삭제는 드물다")이고, 무엇보다 **제거는 (5)가 재는
 * push 경로가 아니다** — 그 경로는 순수한 추가다.
 */
export class TimelineTracker {
  private min = Number.POSITIVE_INFINITY
  private max = Number.NEGATIVE_INFINITY
  private stale = false

  /** 새 항목 하나. O(1). */
  observe(createdAt: string): void {
    const t = toMs(createdAt)
    if (t < this.min) this.min = t
    if (t > this.max) this.max = t
  }

  /** 무언가 사라졌다 — 경계가 좁아졌을 수 있으니 다음 읽기에서 다시 만든다. */
  invalidate(): void {
    this.stale = true
  }

  /** 그래프가 비워졌다(resync 직전 / clear). */
  reset(): void {
    this.min = Number.POSITIVE_INFINITY
    this.max = Number.NEGATIVE_INFINITY
    this.stale = false
  }

  /** 현재 경계. 재계산이 필요할 때만(=제거가 있었을 때만) 전체를 훑는다. */
  bounds(
    nodes: Map<string, Node>,
    edges: Map<string, Edge>,
    findings: Map<string, Finding>,
  ): TimelineBounds | null {
    if (this.stale) {
      const full = timelineBounds(nodes, edges, findings)
      this.min = full ? full.min : Number.POSITIVE_INFINITY
      this.max = full ? full.max : Number.NEGATIVE_INFINITY
      this.stale = false
    }
    if (this.min === Number.POSITIVE_INFINITY) return null
    return { min: this.min, max: this.max }
  }
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
