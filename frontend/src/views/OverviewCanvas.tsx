/**
 * OverviewCanvas — cosmos.gl WebGL overview ([7-A]).
 *
 * [7-D] is why this looks the way it does: the node/edge bodies never enter React
 * state. The component subscribes to `seq` (one aggregate that ticks once per
 * flush), rebuilds typed arrays from the module-scope maps, and hands them
 * straight to the GPU. Nothing here re-renders per node.
 *
 * ★ [7-D] 레이아웃 수명주기 (TASK N, 실측으로 확정된 설계):
 *
 * cosmos.gl 3.3.0 re-initialises its simulation state whenever point positions
 * change while the force simulation is enabled. Measured on a real GPU, that is a
 * **fixed ~130ms per position change** — independent of graph size (10 nodes cost
 * the same as 10,000), and unavoidable while the simulation is on (pause() does
 * not dodge it; toggling the config just moves the cost to the ~200ms re-enable).
 * With the simulation off the same update costs 0.72ms — 180× cheaper.
 *
 * So the layout runs as a lifecycle rather than continuously:
 *
 *   INGESTING  events arriving → simulation off. Appends take the 0.72ms path and
 *              new nodes are seeded beside the neighbours they connect to, so the
 *              graph stays readable before any layout runs. This is the state the
 *              [15] KPIs are met in.
 *   SETTLING   quiet for SETTLE_DEBOUNCE_MS → simulation on, forces arrange the
 *              graph. The ~200ms re-enable stall is paid here, while idle, where
 *              it cannot collide with the user's pan/zoom.
 *   FROZEN     converged → simulation off, positions frozen, pan/zoom stays cheap.
 *
 * Any new event returns to INGESTING immediately. What the user sees: nodes appear
 * beside their neighbours the moment the AI pushes them, and the graph tidies
 * itself once the AI stops — 1000 reflows a second was never readable anyway.
 *
 * Anchor highlight ([23-F] TASK 7) is drawn as a colour/size overlay on existing
 * points — no node or edge is added, so graph topology stays untouched ([23-B]).
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Graph as CosmosGraph } from '@cosmos.gl/graph'
import { useStrings } from '../i18n'
import { consumeGraphDelta, graphData, useGraphStore } from '../stores/graphStore'
import { IncrementalCosmosArrays, idAtIndex, type GraphArrays } from './graphAdapter'
import { visibleLinks, visibleNodeIds } from './temporal'
import { pickTrackedIndices, selectLabels, type LabelPlacement } from './labels'

const ANCHOR_RGBA: [number, number, number, number] = [1, 0.85, 0.2, 1] // amber — gold
const ANCHOR_SIZE_BOOST = 1.8

/** Label re-placement interval. See refreshLabels — this is a GPU readback. */
const LABEL_REFRESH_MS = 80

/**
 * Quiet time before the layout settles ([7-D] SETTLING).
 *
 * Long enough that a burst of pushes does not trigger a settle mid-burst (each
 * one costs the ~200ms re-enable), short enough that the graph tidies itself
 * while the reader is still looking at it.
 */
const SETTLE_DEBOUNCE_MS = 500

/** Energy for the settle pass — enough to arrange, not enough to fling. */
const SETTLE_ALPHA = 0.5

/**
 * Hard ceiling on a settle pass ([7-D] "수렴 또는 유한 틱").
 *
 * Measured: cosmos.gl's simulation does not reach onSimulationEnd on its own here
 * — it was still ticking after 25s — so waiting for natural convergence would
 * leave the simulation on indefinitely, which is exactly the state that costs
 * ~130ms per push. A bounded pass gives a readable layout and a predictable
 * return to the cheap path.
 */
const SETTLE_MAX_MS = 2000

type LayoutPhase = 'ingesting' | 'settling' | 'frozen'

/**
 * Zero-cost timing hook for the [15] perf harness.
 *
 * Inert unless the harness arms `window.__perf`, so production pays one property
 * read. Kept rather than deleted with the rest of TASK N's scaffolding: this is
 * how the settle stall gets measured and reported honestly instead of estimated.
 */
function mark(bucket: string, ms: number): void {
  const w = window as unknown as { __perf?: Record<string, { n: number; ms: number }> }
  if (!w.__perf) return
  const slot = (w.__perf[bucket] ??= { n: 0, ms: 0 })
  slot.n += 1
  slot.ms += ms
}

function timed<T>(bucket: string, fn: () => T): T {
  const w = window as unknown as { __perf?: unknown }
  if (!w.__perf) return fn()
  const start = performance.now()
  const out = fn()
  mark(bucket, performance.now() - start)
  return out
}

export interface OverviewCanvasProps {
  /** Anchor ids of the selected finding — highlighted, never added to the graph. */
  highlighted?: readonly string[]
  onSelectNode?: (id: string) => void
}

/**
 * Positions for exactly the nodes that exist now.
 *
 * Rebuilt from the live arrays rather than updated in place: a deleted node must
 * not leave its position behind. The map is the session's authoritative copy
 * while the simulation is off, so carrying dead ids would leak steadily across a
 * long run of add/delete churn — and a stale entry could later anchor a
 * neighbour's seed to where a node used to be.
 *
 * A non-finite coordinate is dropped rather than stored: cosmos reads an absent
 * point back as NaN by design, and NaN must never become a seed anchor.
 */
export function retainLivePositions(built: GraphArrays): Map<string, readonly [number, number]> {
  const live = new Map<string, readonly [number, number]>()
  for (let i = 0; i < built.ids.length; i += 1) {
    const x = built.pointPositions[i * 2]
    const y = built.pointPositions[i * 2 + 1]
    if (Number.isFinite(x) && Number.isFinite(y)) live.set(built.ids[i], [x, y])
  }
  return live
}

/** Filter dim colour — dark grey, low alpha, so a non-match recedes ([5-C]). */
const DIM_RGBA: [number, number, number, number] = [0.25, 0.28, 0.33, 0.18]

/**
 * [M3c] Temporal fade — a node not yet created at the scrub cutoff is nearly
 * invisible (a faint ghost of what is coming), applied last so it wins over the
 * filter dim and any anchor: a future node is future regardless of overlay
 * (precedence anchor > temporal-fade > dim holds *within* the cutoff, where the
 * fade does not act).
 */
const TEMPORAL_FADE_RGBA: [number, number, number, number] = [0.25, 0.28, 0.33, 0.05]

/**
 * Parse an allowlisted colour to cosmos RGBA (0..1), or null if unparseable.
 *
 * The server already validated the string is hex or rgb/rgba ([11]), so this only
 * has to convert the accepted forms; anything it cannot read is skipped rather
 * than crashing the render.
 */
export function parseColor(value: string): [number, number, number, number] | null {
  const hex = value.trim()
  if (hex.startsWith('#')) {
    let r = hex.slice(1)
    if (r.length === 3) r = r[0] + r[0] + r[1] + r[1] + r[2] + r[2]
    if (r.length !== 6 && r.length !== 8) return null
    const n = (i: number) => parseInt(r.slice(i, i + 2), 16) / 255
    const a = r.length === 8 ? n(6) : 1
    return [n(0), n(2), n(4), a]
  }
  const m = value.match(/rgba?\(([^)]+)\)/)
  if (m) {
    const parts = m[1].split(',').map((p) => Number(p.trim()))
    if (parts.length < 3 || parts.some((p) => Number.isNaN(p))) return null
    return [parts[0] / 255, parts[1] / 255, parts[2] / 255, parts[3] ?? 1]
  }
  return null
}

/**
 * Recolour/resize points for the overlay layers ([5-C] dim, [5-D] AI style, [23-F] anchor).
 *
 * ★ The three overlays are painted bottom-to-top, and that order *is* the
 * precedence rule (coordinator-confirmed): **anchor > AI-style > dim**.
 *  1. Filter dim ([5-C]) greys every node the shared filter does not match.
 *  2. AI styles ([5-D]) recolour/resize their target ids — over the dim, so an
 *     AI highlight shows through a filter, but stacked among themselves so a later
 *     apply_style wins over an earlier one on the same node.
 *  3. Anchor highlight ([23-F]) paints the selected finding's anchors gold, on
 *     top of everything: an explicit human selection beats an AI style beats a
 *     filter. All colour-only — no topology change, so structureSeq is untouched
 *     and the overview stays on the cheap overlay path ([7-D]).
 *
 * ``dim`` is passed only when a filter is active; ``aiStyles`` is the applied
 * styles in insertion order (later last, so later wins).
 */
export function applyHighlight(
  arrays: GraphArrays,
  highlighted: readonly string[],
  dim?: { visibleIds: ReadonlySet<string> } | null,
  aiStyles?: readonly { ids: string[]; style: { color?: string; size?: number } }[],
  temporalVisible?: ReadonlySet<string> | null,
): { colors: Float32Array; sizes: Float32Array } {
  const colors = Float32Array.from(arrays.pointColors)
  const sizes = Float32Array.from(arrays.pointSizes)
  if (dim) {
    for (let i = 0; i < arrays.ids.length; i += 1) {
      if (!dim.visibleIds.has(arrays.ids[i])) colors.set(DIM_RGBA, i * 4)
    }
  }
  if (aiStyles) {
    for (const { ids, style } of aiStyles) {
      const rgba = style.color ? parseColor(style.color) : null
      for (const id of ids) {
        const index = arrays.indexById.get(id)
        if (index === undefined) continue
        if (rgba) colors.set(rgba, index * 4)
        if (style.size != null) sizes[index] = style.size
      }
    }
  }
  for (const id of highlighted) {
    const index = arrays.indexById.get(id)
    if (index === undefined) continue
    colors.set(ANCHOR_RGBA, index * 4)
    sizes[index] = sizes[index] * ANCHOR_SIZE_BOOST
  }
  // [M3c] Applied last: a node created after the scrub cutoff fades to a ghost,
  // over any dim/anchor above. null = live (no temporal overlay).
  if (temporalVisible) {
    for (let i = 0; i < arrays.ids.length; i += 1) {
      if (!temporalVisible.has(arrays.ids[i])) colors.set(TEMPORAL_FADE_RGBA, i * 4)
    }
  }
  return { colors, sizes }
}

interface Hovered {
  label: string
  type: string
  x: number
  y: number
}

export function OverviewCanvas({ highlighted = [], onSelectNode }: OverviewCanvasProps) {
  const t = useStrings()
  const container = useRef<HTMLDivElement>(null)
  const cosmos = useRef<CosmosGraph | null>(null)
  const arrays = useRef<GraphArrays | null>(null)
  /**
   * [7-D]/M3b — the cosmos arrays, maintained in O(delta). ``arrays.current`` is
   * its snapshot (a view for click/hover/labels); ``inc`` owns the growable buffers
   * so a push appends instead of rebuilding O(N) (M3a: 120ms → O(delta) at 100K).
   */
  const inc = useRef<IncrementalCosmosArrays | null>(null)
  /** Auto-frame until the human takes the camera ([7-A] pan/zoom is theirs). */
  const autoFit = useRef(true)
  const lastLabelRefresh = useRef(0)
  const trailingLabels = useRef<number | undefined>(undefined)
  const refreshLabelsRef = useRef<((force?: boolean) => void) | undefined>(undefined)
  /** ★ [7-D] 레이아웃 수명주기 상태. */
  const phase = useRef<LayoutPhase>('ingesting')
  const settleTimer = useRef<number | undefined>(undefined)
  const settleMaxTimer = useRef<number | undefined>(undefined)
  const settleStartedAt = useRef(0)
  /** Tells a data change from a highlight-only re-render — see the data effect. */
  const lastSeq = useRef<number | undefined>(undefined)
  /** Tells a structural change (add/remove) from a property/finding-only one. */
  const lastStructureSeq = useRef<number | undefined>(undefined)
  /** [M3c] Tells a scrub (temporal cutoff moved) from a highlight-only re-render —
   * a scrub re-filters the links, a highlight does not. */
  const lastTemporalCutoff = useRef<number | null | undefined>(undefined)
  /**
   * [KI-1] cosmos.gl finishes its WebGL device/points init asynchronously, so a
   * data-apply that beats `graph.ready` throws. `readyDeferred` remembers that
   * we already parked on `ready`, and bumping `cosmosReadyTick` re-runs the data
   * effect once it resolves — see the readiness gate there.
   */
  const readyDeferred = useRef(false)
  const [cosmosReadyTick, setCosmosReadyTick] = useState(0)
  /**
   * Our authoritative copy of where each node sits.
   *
   * While the simulation is off it is the only copy that moves, so appends can
   * preserve every existing node exactly. The simulation owns positions only
   * during SETTLING, and they are read back once at the end of it.
   */
  const positions = useRef(new Map<string, readonly [number, number]>())
  const onSelect = useRef(onSelectNode)
  onSelect.current = onSelectNode

  /**
   * Keep the settling layout inside the view.
   *
   * The force simulation expands the seed cloud for a second or two after data
   * arrives. Framing once when the first node landed is not enough — the nodes
   * then drift out of the fitted view, taking their labels with them. So re-fit
   * while the layout moves, and stop the moment the user pans or zooms.
   *
   * duration 0: an animated fit every tick would fight itself.
   */
  const keepFramed = useCallback(() => {
    const graph = cosmos.current
    if (!graph || !autoFit.current) return
    if (!arrays.current || arrays.current.ids.length === 0) return // fitView throws on empty
    // enableSimulation=false, and it is not optional: the parameter defaults to
    // true ("run the simulation during the zoom transition"), and keepFramed runs
    // on *every* simulation tick — so the default has the framing re-energising
    // the simulation forever. Alpha never decays, onSimulationEnd never fires,
    // and SETTLING never reaches FROZEN. Duration is 0, so there is no transition
    // to run a simulation for anyway.
    timed('fitView', () => graph.fitView(0, undefined, false))
  }, [])

  const [labels, setLabels] = useState<LabelPlacement[]>([])
  const [hovered, setHovered] = useState<Hovered | null>(null)

  /**
   * Read the simulation's work back into our copy ([7-D] SETTLING → FROZEN).
   *
   * Only worth doing when the simulation has actually moved things. An absent
   * point reads back as NaN by design; those ids are dropped rather than stored,
   * so a removed node cannot come back as a NaN anchor for its neighbours.
   */
  const capturePositions = useCallback(() => {
    const graph = cosmos.current
    const built = arrays.current
    if (!graph || !built) return
    const flat = timed('capturePositions', () => graph.getPointPositions())
    for (let i = 0; i < built.ids.length; i += 1) {
      const x = flat[i * 2]
      const y = flat[i * 2 + 1]
      if (Number.isFinite(x) && Number.isFinite(y)) positions.current.set(built.ids[i], [x, y])
    }
  }, [])

  /**
   * End a settle pass and freeze ([7-D] SETTLING → FROZEN).
   *
   * Reached either by convergence or by the SETTLE_MAX_MS ceiling, whichever
   * comes first. pause() rather than stop(): stop() resets the simulation state,
   * and the whole point is to keep the arrangement it just produced.
   */
  const finishSettle = useCallback(() => {
    const graph = cosmos.current
    if (!graph || phase.current !== 'settling') return
    // Claim the transition *before* pause(): pause() fires onSimulationEnd
    // synchronously, which re-enters here, and a phase still reading 'settling'
    // would let the whole body run twice (double capture, double metric).
    phase.current = 'frozen'
    if (settleMaxTimer.current !== undefined) {
      window.clearTimeout(settleMaxTimer.current)
      settleMaxTimer.current = undefined
    }
    graph.pause()
    capturePositions()
    graph.setConfigPartial({ enableSimulation: false })
    // How long the layout took to become readable — its own [15] metric, not
    // folded into push→display, which it is not part of.
    mark('settle:total(→readable)', performance.now() - settleStartedAt.current)
    keepFramed()
    // Through the ref: refreshLabels is declared below this callback. Forced,
    // because this is the layout's final position — the throttle would otherwise
    // drop it and leave the labels where the settle started.
    refreshLabelsRef.current?.(true)
  }, [capturePositions, keepFramed])
  const finishSettleRef = useRef(finishSettle)
  finishSettleRef.current = finishSettle

  /** Leave the simulation off ([7-D] INGESTING) — the cheap path for live pushes. */
  const enterIngesting = useCallback(() => {
    const graph = cosmos.current
    if (!graph) return
    if (phase.current === 'settling') {
      // Interrupted mid-layout: keep whatever the forces achieved so far rather
      // than discarding it, so an interrupted settle still moves the graph on.
      if (settleMaxTimer.current !== undefined) {
        window.clearTimeout(settleMaxTimer.current)
        settleMaxTimer.current = undefined
      }
      graph.pause()
      capturePositions()
    }
    if (phase.current !== 'ingesting') {
      timed('setConfig(sim off)', () => graph.setConfigPartial({ enableSimulation: false }))
      phase.current = 'ingesting'
    }
  }, [capturePositions])

  /**
   * Arrange the graph once the pushes stop ([7-D] SETTLING).
   *
   * Debounced: every entry costs the ~200ms simulation re-init, so settling in
   * the middle of a burst would pay it repeatedly and be undone by the next push.
   */
  const scheduleSettle = useCallback(() => {
    if (settleTimer.current !== undefined) window.clearTimeout(settleTimer.current)
    settleTimer.current = window.setTimeout(() => {
      settleTimer.current = undefined
      const graph = cosmos.current
      if (!graph || !arrays.current || arrays.current.ids.length === 0) return
      phase.current = 'settling'
      settleStartedAt.current = performance.now()
      // The stall lives here, on purpose: idle, never overlapping an interaction.
      timed('settle:enable(sim on)', () => graph.setConfigPartial({ enableSimulation: true }))
      timed('settle:start', () => graph.start(SETTLE_ALPHA))
      // Bounded: cosmos does not converge to onSimulationEnd on its own here, and
      // a simulation left running is a ~130ms tax on every subsequent push.
      settleMaxTimer.current = window.setTimeout(() => {
        settleMaxTimer.current = undefined
        finishSettleRef.current()
      }, SETTLE_MAX_MS)
    }, SETTLE_DEBOUNCE_MS)
  }, [])

  // Aggregate subscription ([7-D]): ticks once per flush, not once per node.
  const seq = useGraphStore((s) => s.seq)
  /** [7-A] 재시드 게이트 — only add/remove moves the layout. */
  const structureSeq = useGraphStore((s) => s.structureSeq)
  const nodeCount = useGraphStore((s) => s.nodeCount)
  /** [5-C] shared filter — its visible set drives the dim overlay ([7-D] overlay path). */
  const filter = useGraphStore((s) => s.filter)
  // A filter is active only with a non-empty expression: an empty one means
  // "show all", so nothing dims. Memoised on `filter` (a stable reference until it
  // actually changes) so it does not thrash the data effect's deps every render.
  const dim = useMemo(
    () => (filter.expression.trim() ? { visibleIds: filter.visibleIds } : null),
    [filter],
  )
  /** [5-D] AI styles, in apply order (later wins). Middle overlay layer. */
  const aiStyleMap = useGraphStore((s) => s.aiStyles)
  const aiStyles = useMemo(() => [...aiStyleMap.values()], [aiStyleMap])
  /** [M3c] the created_at cutoff (epoch ms) the view is held at, or null for live. */
  const temporalCutoff = useGraphStore((s) => s.temporalCutoff)
  /**
   * [M3c] the node ids visible at the cutoff. Recomputed only when the cutoff or the
   * node *set* changes (created_at is immutable, so an update never moves a node in
   * time) — a filter-dim-level O(N), off the [7-D] structural path.
   */
  const temporalVisible = useMemo(
    () => visibleNodeIds(graphData.nodes, temporalCutoff),
    [temporalCutoff, structureSeq],
  )

  /** [7-A] LOD — recomputed on zoom/tick, never inside a per-node render. */
  /**
   * @param force Skip the throttle. Use for the moments the label must be right
   *   rather than soon: the layout settling, or the data changing under it.
   */
  const refreshLabels = useCallback((force = false) => {
    const graph = cosmos.current
    if (!graph || !arrays.current) return

    // Throttled on purpose. getTrackedPointPositionsMap reads back from the GPU,
    // and the simulation ticks at frame rate — doing this every tick stalls the
    // main thread (chromium logs "GPU stall due to ReadPixels"), which is the
    // failure [7-D] exists to prevent. Labels do not need 60fps; text that
    // re-places ~12 times a second reads as smooth.
    const now = performance.now()
    if (!force && now - lastLabelRefresh.current < LABEL_REFRESH_MS) {
      // Trailing edge, and it is load-bearing rather than polish. With the
      // simulation off ([7-D] INGESTING/FROZEN) nothing ticks, so a throttled
      // call that simply returned would be the *last* word — the labels for a
      // node pushed inside the window would never appear at all. Re-arm instead.
      if (trailingLabels.current === undefined) {
        trailingLabels.current = window.setTimeout(() => {
          trailingLabels.current = undefined
          refreshLabelsRef.current?.(true)
        }, LABEL_REFRESH_MS)
      }
      return
    }
    if (trailingLabels.current !== undefined) {
      window.clearTimeout(trailingLabels.current)
      trailingLabels.current = undefined
    }
    lastLabelRefresh.current = now

    const box = container.current?.getBoundingClientRect()
    // ★ TASK N 계측: readback 만 따로 — 이게 스톨의 후보 1번이다.
    const tracked = timed('readback(getTrackedPointPositionsMap)', () =>
      graph.getTrackedPointPositionsMap(),
    )
    timed('selectLabels+setLabels', () =>
      setLabels(
        selectLabels({
          tracked,
          arrays: arrays.current!,
          zoom: graph.getZoomLevel(),
          labelOf: (id) => graphData.getNode(id)?.label,
          toScreen: (position) => graph.spaceToScreenPosition(position),
          viewport: box ? { width: box.width, height: box.height } : undefined,
        }),
      ),
    )
    mark('refreshLabels(calls)', 0)
  }, [])
  // The trailing timer fires after this callback's closure was created, so it
  // reaches the function through a ref rather than capturing a stale one.
  refreshLabelsRef.current = refreshLabels

  useEffect(() => {
    if (!container.current || cosmos.current) return
    cosmos.current = new CosmosGraph(container.current, {
      backgroundColor: '#020617',
      // ★ Starts off ([7-D] INGESTING). The app opens empty and fills from a
      // resync or live pushes; leaving the simulation on would charge ~130ms per
      // position change for the whole load. SETTLING turns it on when it matters.
      enableSimulation: false,
      pointSizeScale: 1,
      linkWidthScale: 1,
      linkDefaultColor: '#334155',
      simulationGravity: 0.1,
      simulationRepulsion: 0.6,
      renderHoveredPointRing: true,
      hoveredPointRingColor: '#38bdf8',
      onClick: (index) => {
        if (index === undefined || !arrays.current) return
        const id = idAtIndex(arrays.current, index)
        if (id) onSelect.current?.(id)
      },
      // [7-A] 노드 hover → 라벨 tooltip.
      onPointMouseOver: (index, position) => {
        if (!arrays.current || !cosmos.current) return
        const id = idAtIndex(arrays.current, index)
        const node = id ? graphData.getNode(id) : undefined
        if (!node) return
        const [x, y] = cosmos.current.spaceToScreenPosition(position)
        setHovered({ label: node.label, type: node.type, x, y })
      },
      onPointMouseOut: () => setHovered(null),
      onZoomStart: (_event, userDriven) => {
        // The moment the human pans or zooms, the camera is theirs.
        if (userDriven) autoFit.current = false
      },
      onZoom: () => refreshLabels(),
      onSimulationTick: () => {
        keepFramed()
        refreshLabels()
      },
      // ★ [7-D] SETTLING → FROZEN. Converged on its own — take the layout and put
      // the simulation away so the next push is back on the 0.72ms path. (The
      // SETTLE_MAX_MS ceiling calls the same path; whichever lands first wins.)
      onSimulationEnd: () => finishSettleRef.current(),
    })
    return () => {
      if (settleTimer.current !== undefined) window.clearTimeout(settleTimer.current)
      if (settleMaxTimer.current !== undefined) window.clearTimeout(settleMaxTimer.current)
      if (trailingLabels.current !== undefined) window.clearTimeout(trailingLabels.current)
      cosmos.current?.destroy()
      cosmos.current = null
      inc.current = null // a new cosmos gets a fresh builder ([7-D]/M3b baseline)
      arrays.current = null
    }
  }, [refreshLabels, keepFramed])

  useEffect(() => {
    const graph = cosmos.current
    if (!graph) return

    // ★ [KI-1] cosmos.gl builds its WebGL device and points module asynchronously.
    // Calling setPointPositions before `graph.ready` resolves throws inside cosmos
    // ("this.points" is momentarily undefined even though the device gate passed) —
    // an uncaught render throw. Gate the apply on the public readiness signal and
    // re-run this effect once ready (via cosmosReadyTick), so a push that arrives
    // before init finishes is applied the instant cosmos is ready instead of
    // crashing. Deterministic: no reliance on init winning the race.
    if (!graph.isReady) {
      if (!readyDeferred.current) {
        readyDeferred.current = true
        graph.ready
          .then(() => {
            readyDeferred.current = false
            setCosmosReadyTick((t) => t + 1)
          })
          .catch(() => {
            readyDeferred.current = false
          })
      }
      return
    }

    // ★ Only a change to the node/edge *set* may move the layout.
    //
    // Three ways in, and they cost wildly different things:
    //  - highlighted changed only  → recolour what is already built.
    //  - seq changed, set did not  → a property was corrected or a finding was
    //    recorded. Rebuild so the new label/colour shows, but do not touch
    //    positions and do not settle: gating this on `seq` is what made a settled
    //    graph lurch for ~2.8s every time the AI recorded gold — the single most
    //    common thing this tool does ([23-B] finding 은 위상을 바꾸지 않는다).
    //  - structureSeq changed      → nodes/edges arrived or left. Re-seed, settle.
    const dataChanged = lastSeq.current !== seq
    const structureChanged = lastStructureSeq.current !== structureSeq
    // [M3c] a scrub moved the cutoff — future edges must vanish / reappear, which
    // means re-uploading the (temporal-filtered) link array. A highlight-only change
    // does not touch links, so it is told apart here.
    const cutoffChanged = lastTemporalCutoff.current !== temporalCutoff
    lastSeq.current = seq
    lastStructureSeq.current = structureSeq
    lastTemporalCutoff.current = temporalCutoff

    // [M3c] The links cosmos should draw: filtered to created_at ≤ cutoff while
    // scrubbing (future edges hidden), or the full set when live. A pure overlay —
    // no structural rebuild, so scrubbing stays off the [7-D] structural path.
    const linksFor = (built: GraphArrays): Float32Array =>
      temporalCutoff !== null && temporalVisible
        ? visibleLinks(graphData.edges, built.indexById, temporalVisible, temporalCutoff)
        : built.links

    if (!dataChanged && !structureChanged) {
      const built = arrays.current
      if (!built) return
      const { colors, sizes } = timed('overlay:applyHighlight', () =>
        applyHighlight(built, highlighted, dim, aiStyles, temporalVisible),
      )
      graph.setPointColors(colors)
      graph.setPointSizes(sizes)
      // A scrub re-filters the links; a highlight leaves them. Positions untouched
      // either way, so a FROZEN graph stays frozen — and no settle is armed.
      if (cutoffChanged) graph.setLinks(linksFor(built))
      timed('overlay:render', () => graph.render(undefined, 0))
      return
    }

    const resolvePrev = (id: string) => positions.current.get(id)

    if (!structureChanged) {
      // The set is unchanged, so every node keeps its exact position — but its
      // type/layer/placeholder may have changed, and those decide its colour.
      // ★ O(delta): recolour/resize only the touched nodes in place; positions are
      // not moved (no setPointPositions — the ~130ms call — and no settle).
      const delta = consumeGraphDelta()
      if (!inc.current) return
      const built = timed('overlay:touch', () => inc.current!.touch(delta.touchedNodes, graphData.nodes))
      arrays.current = built
      const { colors, sizes } = applyHighlight(built, highlighted, dim, aiStyles, temporalVisible)
      graph.setPointColors(colors)
      graph.setPointSizes(sizes)
      if (cutoffChanged) graph.setLinks(linksFor(built))
      timed('overlay:render', () => graph.render(undefined, 0))
      // A renamed node needs its label redrawn; positions did not move.
      timed('overlay:refreshLabels', () => refreshLabels())
      return
    }

    const effectStart = performance.now()
    // ★ Data changed → back to the cheap path before touching positions.
    enterIngesting()

    const delta = consumeGraphDelta()
    // A freshly-created builder (mount / remount) rebuilds from the full store once
    // to establish its baseline — a delta only describes a change, not the whole set.
    const freshInc = inc.current === null
    if (freshInc) inc.current = new IncrementalCosmosArrays()
    const needsRebuild =
      freshInc || delta.rebuild || delta.removedNodes.length > 0 || delta.removedEdges.length > 0
    let built: GraphArrays
    if (needsRebuild) {
      // A removal / resync: rebuild from scratch (rare — the streaming path is adds).
      // retainLivePositions keeps existing placements and prunes the stale ones,
      // so positions.current never grows without bound ([7-D] session-lifetime map).
      built = timed('effect:rebuild', () =>
        inc.current!.rebuild(graphData.nodes, graphData.edges, resolvePrev),
      )
      positions.current = retainLivePositions(built)
    } else {
      // ★ O(delta) ([7-D] "incremental update — no full reset"): append the new
      // nodes/edges and re-size only the nodes whose degree changed, instead of
      // rebuilding all N (M3a: 120ms/push at 100K).
      built = timed('effect:applyDelta', () =>
        inc.current!.applyAdd(delta, graphData.nodes, graphData.edges, resolvePrev),
      )
      // Record just the new nodes' seed positions (O(delta)) so a later seed or a
      // rebuild keeps them exactly where they first landed.
      for (const id of delta.addedNodes) {
        const i = built.indexById.get(id)
        if (i === undefined) continue
        const x = built.pointPositions[i * 2]
        const y = built.pointPositions[i * 2 + 1]
        if (Number.isFinite(x) && Number.isFinite(y)) positions.current.set(id, [x, y])
      }
      // Self-healing: if the incremental count ever drifts from the store (an event
      // the delta missed, a race), a full rebuild reconciles it — a desync can never
      // render the wrong points, only cost one slower flush.
      if (built.ids.length !== graphData.nodes.size) {
        built = inc.current!.rebuild(graphData.nodes, graphData.edges, resolvePrev)
        positions.current = retainLivePositions(built)
      }
    }
    arrays.current = built
    const { colors, sizes } = timed('effect:applyHighlight', () => applyHighlight(built, highlighted, dim, aiStyles, temporalVisible))

    // dontRescale=true. buildGraphArrays already seeds inside spaceSize, and it
    // keeps input space == internal space — which matters because
    // spaceToScreenPosition applies the input→internal rescale itself, while
    // getTrackedPointPositionsMap hands back already-internal coordinates. With
    // the rescale on, feeding one to the other applies it twice and every label
    // lands far off screen.
    graph.setPointPositions(built.pointPositions, true)
    graph.setPointColors(colors)
    graph.setPointSizes(sizes)
    graph.setLinks(linksFor(built)) // [M3c] temporal-filtered while scrubbing, else full
    // render(undefined, 0): the default uses config.transitionDuration — 800ms —
    // which starts an animation on every flush *and* pauses the simulation for
    // its duration. Neither is wanted here; 0 snaps the data in.
    timed('effect:render', () => graph.render(undefined, 0))
    // [7-A] track only the top-N by degree — those are the ones that get labels.
    // After render(), not before: tracking allocates against pointsTextureSize,
    // which render() is what sets. Called earlier it silently no-ops and the
    // tracked map stays empty forever.
    timed('effect:trackPointPositions', () =>
      graph.trackPointPositionsByIndices(pickTrackedIndices(built)),
    )
    // cosmos fits on init, but the app starts empty, so that fit framed nothing.
    // keepFramed carries the view from here until the user takes over.
    timed('effect:keepFramed', () => keepFramed())
    // Not forced. force here bypassed the 80ms throttle on every flush, paying a
    // GPU readback per push — the very stall 7d.1 added the throttle to stop. The
    // trailing edge guarantees the labels still land while the simulation is off.
    timed('effect:refreshLabels', () => refreshLabels())
    mark('effect(total)', performance.now() - effectStart)

    // The graph just changed, so any settle in flight is stale — re-arm it.
    scheduleSettle()
    // seq drives the graph data; highlighted redraws the overlay only.
  }, [seq, structureSeq, highlighted, dim, aiStyles, temporalCutoff, temporalVisible, refreshLabels, keepFramed, enterIngesting, scheduleSettle, cosmosReadyTick])

  return (
    <div className="relative h-full w-full overflow-hidden" data-testid="overview-canvas">
      <div ref={container} className="h-full w-full" data-testid="cosmos-container" />

      {/* [7-A] label overlay — cosmos has no labels of its own. Plain React text,
          so an AI-supplied label is escaped, never parsed as markup ([11]). */}
      <div
        className="pointer-events-none absolute inset-0"
        aria-hidden="true"
        data-testid="label-overlay"
      >
        {labels.map((placement) => (
          <span
            key={placement.id}
            data-testid="node-label"
            className="absolute -translate-x-1/2 translate-y-2 whitespace-nowrap text-[10px] text-slate-300 drop-shadow-[0_1px_2px_rgba(0,0,0,0.9)]"
            style={{ left: placement.x, top: placement.y }}
          >
            {placement.label}
          </span>
        ))}
      </div>

      {hovered && (
        <div
          className="pointer-events-none absolute z-10 -translate-x-1/2 -translate-y-full rounded bg-slate-800 px-2 py-1 text-[10px] text-slate-100 shadow-lg"
          style={{ left: hovered.x, top: hovered.y - 8 }}
          data-testid="overview-tooltip"
        >
          <span className="font-semibold">{hovered.label}</span>
          <span className="ml-1 text-slate-400">{hovered.type}</span>
        </div>
      )}

      {nodeCount === 0 && (
        <p
          className="pointer-events-none absolute inset-0 flex items-center justify-center text-xs text-slate-600"
          data-testid="overview-empty"
        >
          {t.overviewEmpty}
        </p>
      )}
    </div>
  )
}
