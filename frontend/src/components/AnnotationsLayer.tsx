/**
 * AnnotationsLayer — the AI's screen-space notes ([5-D], [11]).
 *
 * add_annotation broadcasts annotation.add{annotation_id, x, y, text, ttl}; the
 * store holds them and TTL expiry removes them. Each note is an absolutely
 * positioned label at its (x, y) over the viewport.
 *
 * ★ text is AI-written, rendered as JSX text so React escapes it ([11]). No
 * dangerouslySetInnerHTML. The layer is pointer-events-none so it never steals
 * clicks from the graph beneath it.
 */

import { useGraphStore } from '../stores/graphStore'

export function AnnotationsLayer() {
  const annotations = useGraphStore((s) => s.annotations)
  if (annotations.size === 0) return null

  return (
    <div
      className="pointer-events-none absolute inset-0 z-20"
      aria-hidden="true"
      data-testid="annotations-layer"
    >
      {[...annotations.entries()].map(([id, note]) => (
        <div
          key={id}
          data-testid="annotation"
          className="absolute -translate-x-1/2 -translate-y-1/2 whitespace-nowrap rounded bg-amber-500/90 px-1.5 py-0.5 text-[10px] font-medium text-slate-950 shadow"
          style={{ left: note.x, top: note.y }}
        >
          {note.text}
        </div>
      ))}
    </div>
  )
}
