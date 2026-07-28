/**
 * Rendering safety helpers ([11]).
 *
 * Everything the UI shows — labels, properties, finding bodies, evidence,
 * citations — is untrusted: it arrives from AI pushes or file import ([11]).
 * React escapes text nodes for us, so plain JSX is safe and
 * dangerouslySetInnerHTML is banned. Links are the exception React does not
 * fully cover, which is what `isClickableUrl` is for.
 */

/**
 * [11] href scheme allowlist — only http/https render as a clickable anchor.
 *
 * [5-F] lets a citation's source_url be any string ("파일 경로/주소도 허용 —
 * 반드시 http 일 필요 없음": trace://0x1400, C:/reports/trace.txt). React blocks
 * `javascript:` on its own, but not `data:text/html`, `vbscript:` and friends —
 * so the allowlist rather than React's single mitigation is the defense. A
 * non-http scheme could not be opened by the browser anyway, so nothing of value
 * is lost by showing it as plain text.
 */
export function isClickableUrl(url: string): boolean {
  let parsed: URL
  try {
    parsed = new URL(url)
  } catch {
    return false // relative paths, C:/..., bare addresses
  }
  return parsed.protocol === 'http:' || parsed.protocol === 'https:'
}

/**
 * [23-B]: properties keys starting with `_` are system-reserved — hidden from the
 * filter DSL's `properties.` access and shown separately in the inspector's
 * evidence section rather than in the properties table. Mirrors the backend's
 * `is_reserved_property`.
 */
export function isReservedProperty(key: string): boolean {
  return key.startsWith('_')
}

export const CITATIONS_PROPERTY = '_citations'
/** [24-C] 이력 — 이전에 유효했던 값. */
export const SUPERSEDED_PROPERTY = '_superseded'
/** [24-B] 변경로그 — 정정이 있었다는 사실만 (틀린 값 자체는 없다). */
export const PROVENANCE_PROPERTY = '_provenance'

const LAYER_PALETTE = [
  '#60a5fa',
  '#f472b6',
  '#34d399',
  '#fbbf24',
  '#a78bfa',
  '#fb7185',
  '#22d3ee',
  '#a3e635',
]

/**
 * Colour for a layer ([23-F] TASK 7 shows a layer colour on each finding row).
 *
 * A user-assigned colour wins ([10-A]: the sidebar lets layers be coloured);
 * until one is set, [9-C]'s LayerInfo.color is null, so fall back to a
 * deterministic hash — the same layer always gets the same colour. Mirrors the
 * automatic palette [7-A] already uses for node types.
 */
export function layerColor(layer: string | null, assigned?: string | null): string | null {
  if (assigned) return assigned
  if (!layer) return null
  let hash = 0
  for (let i = 0; i < layer.length; i += 1) {
    hash = (hash * 31 + layer.charCodeAt(i)) >>> 0
  }
  return LAYER_PALETTE[hash % LAYER_PALETTE.length]
}
