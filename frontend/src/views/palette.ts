/**
 * Colour palettes ([7-A]: 노드 색 = type 별 자동 팔레트 또는 layer 색).
 *
 * Deterministic hashes rather than assigned colours: a type or layer always gets
 * the same colour across sessions, which is what makes a graph re-readable. Same
 * rule the findings panel already uses for layers.
 */

const TYPE_PALETTE: [number, number, number][] = [
  [96, 165, 250],
  [244, 114, 182],
  [52, 211, 153],
  [251, 191, 36],
  [167, 139, 250],
  [251, 113, 133],
  [34, 211, 238],
  [163, 230, 53],
]

/** [5-A] placeholder nodes read as unresolved — dimmed, not a real type colour. */
export const PLACEHOLDER_RGB: [number, number, number] = [100, 116, 139]

export function hashIndex(value: string, buckets: number): number {
  let hash = 0
  for (let i = 0; i < value.length; i += 1) {
    hash = (hash * 31 + value.charCodeAt(i)) >>> 0
  }
  return hash % buckets
}

export function typeColor(type: string): [number, number, number] {
  return TYPE_PALETTE[hashIndex(type, TYPE_PALETTE.length)]
}

export function rgbToCss(rgb: [number, number, number]): string {
  return `rgb(${rgb[0]}, ${rgb[1]}, ${rgb[2]})`
}
