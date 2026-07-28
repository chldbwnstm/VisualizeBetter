/**
 * Overview label selection + LOD ([7-A]).
 *
 * cosmos.gl has no built-in labels — [7-A] says so outright and calls for a
 * separate HTML/CSS overlay layer. This picks *which* labels to draw and where;
 * the overlay itself is plain React, so the text is escaped by default ([11])
 * rather than injected as HTML.
 *
 * [7-A] LOD:
 *   zoom 낮음 → 노드만 (점)
 *   zoom 높음 → 화면 내 상위 N개 노드 라벨 (샘플링)
 *
 * Sampling is by degree: cosmos tracks only the top-N points (see
 * pickTrackedIndices), so hubs get named first and a dense graph stays legible.
 * Positions come from trackPointPositionsByIndices — the API cosmos documents for
 * exactly this — rather than the FBO-based getSampledPointPositionsMap, which
 * returns nothing until its GPU textures happen to be ready.
 *
 * Pure so the sampling, LOD and viewport rules are testable without a GL context.
 */

import type { GraphArrays } from './graphAdapter'

/**
 * Below this zoom the graph reads as a shape, not a list.
 *
 * cosmos fits the view on init, so this doubles as a size rule without needing a
 * node count: a small graph fits at a high zoom and gets labels, while a 100K
 * graph fits at a tiny zoom and stays dots — which is what [7-A] is asking for.
 */
export const LABEL_ZOOM_THRESHOLD = 0.5

/** Draw at most this many labels; more is unreadable and costs layout. */
export const MAX_LABELS = 30

/** Keep labels a little past the edge so they do not pop at the boundary. */
const VIEWPORT_MARGIN = 40

export interface LabelPlacement {
  id: string
  label: string
  /** Screen pixels. */
  x: number
  y: number
}

export interface Viewport {
  width: number
  height: number
}

/**
 * The point indices worth tracking: the biggest N, which is the highest degree N
 * ([7-A] 상위 N개). Deterministic — ties break on index.
 */
export function pickTrackedIndices(arrays: GraphArrays, maxLabels = MAX_LABELS): number[] {
  if (maxLabels <= 0) return []
  return arrays.ids
    .map((_, index) => index)
    .sort((a, b) => (arrays.pointSizes[b] ?? 0) - (arrays.pointSizes[a] ?? 0) || a - b)
    .slice(0, maxLabels)
}

export interface LabelSource {
  /** cosmos getTrackedPointPositionsMap: index → space position. */
  tracked: ReadonlyMap<number, [number, number]>
  arrays: GraphArrays
  zoom: number
  /** Node label text by id — read from the off-React map ([7-D]). */
  labelOf: (id: string) => string | undefined
  /** cosmos spaceToScreenPosition. */
  toScreen: (position: [number, number]) => [number, number]
  viewport?: Viewport
  maxLabels?: number
  zoomThreshold?: number
}

export function selectLabels(source: LabelSource): LabelPlacement[] {
  const {
    tracked,
    arrays,
    zoom,
    labelOf,
    toScreen,
    viewport,
    maxLabels = MAX_LABELS,
    zoomThreshold = LABEL_ZOOM_THRESHOLD,
  } = source

  if (zoom < zoomThreshold) return []
  if (maxLabels <= 0) return []

  const candidates: { index: number; size: number; position: [number, number] }[] = []
  for (const [index, position] of tracked) {
    const size = arrays.pointSizes[index]
    // A tracked index from before the last rebuild, or a removed point.
    if (size === undefined) continue
    if (!Number.isFinite(position[0]) || !Number.isFinite(position[1])) continue
    candidates.push({ index, size, position })
  }
  candidates.sort((a, b) => b.size - a.size || a.index - b.index)

  const placements: LabelPlacement[] = []
  for (const candidate of candidates) {
    if (placements.length >= maxLabels) break
    const id = arrays.ids[candidate.index]
    if (id === undefined) continue
    const label = labelOf(id)
    if (!label) continue
    const [x, y] = toScreen(candidate.position)
    // [7-A] 화면 내 — an off-screen label is wasted DOM.
    if (viewport) {
      if (x < -VIEWPORT_MARGIN || y < -VIEWPORT_MARGIN) continue
      if (x > viewport.width + VIEWPORT_MARGIN || y > viewport.height + VIEWPORT_MARGIN) continue
    }
    placements.push({ id, label, x, y })
  }
  return placements
}
