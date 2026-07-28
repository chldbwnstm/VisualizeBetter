/**
 * TemporalScrubber — the [2-B]/M3 time-axis scrubber.
 *
 * Drag the timeline to hold the view at a past created_at cutoff; press play to
 * watch the graph grow from there to now. It is a pure view overlay ([7-D] like the
 * filter): it sets ``temporalCutoff`` (epoch ms) and the renderers fade whatever was
 * created after it — no reconstruction, no structural change (M3c decision A).
 *
 * ★ Honest limitation (shown in the tooltip): this reveals the *current* graph in
 * creation order. A node deleted before now cannot reappear at a past T, and an
 * updated node shows its current value — this is "how the graph grew", not a
 * faithful time-travel. Full replay (deletes / past values) is the follow-up.
 */

import { useEffect, useMemo, useRef, useState } from 'react'
import { useStrings } from '../i18n'
import { graphData, useGraphStore } from '../stores/graphStore'
import { timelineBounds } from '../views/temporal'

const PLAY_DURATION_MS = 6000

/** Epoch ms → HH:MM:SS local, for the "viewing the past" readout. */
function clock(ms: number): string {
  const d = new Date(ms)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

export function TemporalScrubber() {
  const t = useStrings()
  const structureSeq = useGraphStore((s) => s.structureSeq)
  const findings = useGraphStore((s) => s.findings)
  const cutoff = useGraphStore((s) => s.temporalCutoff)
  const setCutoff = useGraphStore((s) => s.setTemporalCutoff)

  // The track: [earliest, latest] created_at. Recomputed only when the graph grows.
  const bounds = useMemo(
    () => timelineBounds(graphData.nodes, graphData.edges, findings),
    [structureSeq, findings],
  )
  const [playing, setPlaying] = useState(false)
  const rafRef = useRef<number | undefined>(undefined)

  // Play: sweep the cutoff from its current spot (or the start, when live) to the
  // end over PLAY_DURATION_MS, then snap back to live. rAF, so it is smooth and
  // stops itself; it does not depend on `cutoff` (that would restart every tick).
  useEffect(() => {
    if (!playing || !bounds || bounds.max <= bounds.min) {
      setPlaying(false)
      return
    }
    const from = cutoff ?? bounds.min
    const start = performance.now()
    const step = (now: number) => {
      const frac = Math.min(1, (now - start) / PLAY_DURATION_MS)
      const t = from + (bounds.max - from) * frac
      if (frac >= 1) {
        setCutoff(null) // reached now → live
        setPlaying(false)
        return
      }
      setCutoff(t)
      rafRef.current = requestAnimationFrame(step)
    }
    rafRef.current = requestAnimationFrame(step)
    return () => {
      if (rafRef.current !== undefined) cancelAnimationFrame(rafRef.current)
    }
    // Intentionally not `cutoff`: the sweep sets it, and re-running on that restart.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [playing, bounds])

  if (!bounds || bounds.max <= bounds.min) return null // nothing to scrub yet

  const live = cutoff === null
  const value = live ? bounds.max : Math.min(bounds.max, Math.max(bounds.min, cutoff))

  return (
    <div
      className="flex items-center gap-2 text-xs text-slate-400"
      data-testid="temporal-scrubber"
      title={t.temporalTitle}
    >
      <button
        type="button"
        data-testid="temporal-play"
        aria-label={playing ? 'pause' : 'play'}
        onClick={() => setPlaying((p) => !p)}
        className="rounded border border-slate-700 px-2 py-1 text-slate-300 hover:bg-slate-800"
      >
        {playing ? '⏸' : '▶'}
      </button>
      <input
        type="range"
        data-testid="temporal-slider"
        aria-label="timeline"
        min={bounds.min}
        max={bounds.max}
        step={Math.max(1, Math.round((bounds.max - bounds.min) / 1000))}
        value={value}
        onChange={(e) => {
          const t = Number(e.target.value)
          setPlaying(false)
          setCutoff(t >= bounds.max ? null : t) // dragging to the end returns to live
        }}
        className="h-1 w-40 cursor-pointer accent-sky-500"
      />
      {live ? (
        <span data-testid="temporal-status" className="font-semibold text-emerald-400">
          ● LIVE
        </span>
      ) : (
        <>
          <span data-testid="temporal-status" className="font-semibold text-amber-300">
            {t.pastAt(clock(value))}
          </span>
          <button
            type="button"
            data-testid="temporal-live"
            onClick={() => {
              setPlaying(false)
              setCutoff(null)
            }}
            className="rounded border border-slate-700 px-2 py-1 text-slate-300 hover:bg-slate-800"
          >
            {t.backToLive}
          </button>
        </>
      )}
    </div>
  )
}
