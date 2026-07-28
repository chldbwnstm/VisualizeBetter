/**
 * WebSocket client — routes [8-C] traffic between the server and the store.
 *
 * Inbound events go straight to `applyServerEvent`, which queues them for the
 * [7-D] rAF flush. Outbound view.update is throttled to 250ms ([8-C]).
 *
 * Reconnect resync is a stub: the [8-C] M1 procedure is "connect WS (buffering)
 * → fetch seq-tagged /graph.json → drop buffered events at or below the snapshot
 * seq → apply the rest". GET /graph.json belongs to serve, which does not exist
 * yet, so `resync` is left as the seam and the buffering side is in place.
 */

import { useGraphStore } from '../stores/graphStore'
import type { ClientEvent, Edge, Finding, Node, ViewMode, WSEvent } from '../types'

export const VIEW_UPDATE_THROTTLE_MS = 250 // [8-C]
export const DEFAULT_RECONNECT_DELAY_MS = 1000

/**
 * [8-C] heartbeat (KI-1). The client pings this often; if no pong has arrived for
 * HEARTBEAT_DEAD_MS the socket is treated as half-open and force-closed to trigger
 * a reconnect + resync. Short enough that recovery lands well inside the E2E's 30s
 * assertion window; a control message every few seconds is trivial for a local
 * realtime tool.
 */
export const HEARTBEAT_INTERVAL_MS = 5000
export const HEARTBEAT_DEAD_MS = 12000

/** The slice of WebSocket the client needs — lets tests supply a double. */
export interface SocketLike {
  send(data: string): void
  close(): void
  onopen: ((this: unknown, ev: unknown) => unknown) | null
  onclose: ((this: unknown, ev: unknown) => unknown) | null
  onmessage: ((this: unknown, ev: { data: unknown }) => unknown) | null
  onerror: ((this: unknown, ev: unknown) => unknown) | null
}

export interface GraphClientOptions {
  url: string
  /** Injectable so tests can drive a fake socket. */
  createSocket?: (url: string) => SocketLike
  /** Injectable clock for the view.update throttle. */
  now?: () => number
  reconnectDelayMs?: number
  /** Scheduler for the reconnect/heartbeat timers. Returns a handle to cancel. */
  schedule?: (fn: () => void, ms: number) => unknown
  /** Cancels a handle from `schedule` — used to drop a pending reconnect on close. */
  clearScheduled?: (handle: unknown) => void
  autoReconnect?: boolean
  /** [8-C] seq-tagged snapshot endpoint. Derived from the WS url by default. */
  graphUrl?: string
  fetchImpl?: typeof fetch
}

/** GET /graph.json ([8-C] resync 진입점). */
export interface GraphSnapshot {
  seq: number
  nodes: Node[]
  edges: Edge[]
  findings: Finding[]
  layers?: string[]
  focus?: string | null
  active_filter?: string | null
}

/** ws://host/live → http://host/graph.json */
export function graphUrlFrom(wsUrl: string): string {
  return wsUrl.replace(/^ws/, 'http').replace(/\/live$/, '/graph.json')
}

export class GraphClient {
  private socket: SocketLike | null = null
  private lastViewUpdateAt = 0
  private pendingViewUpdate: ClientEvent | null = null
  private closedByUs = false
  private lastPongAt = 0

  readonly url: string
  readonly graphUrl: string
  private readonly createSocket: (url: string) => SocketLike
  private readonly now: () => number
  private readonly reconnectDelayMs: number
  private readonly schedule: (fn: () => void, ms: number) => unknown
  private readonly clearScheduled: (handle: unknown) => void
  private readonly autoReconnect: boolean
  private readonly fetchImpl: typeof fetch
  /** Handle of a reconnect queued by an unexpected onclose, so close() can cancel it. */
  private reconnectHandle: unknown = undefined

  connected = false
  reconnectAttempts = 0
  /** Notified whenever `connected` changes — [9-B] ConnectionBar shows 접속 상태. */
  onConnectionChange?: (connected: boolean) => void

  constructor(options: GraphClientOptions) {
    this.url = options.url
    this.graphUrl = options.graphUrl ?? graphUrlFrom(options.url)
    this.createSocket =
      options.createSocket ?? ((url) => new WebSocket(url) as unknown as SocketLike)
    this.now = options.now ?? (() => Date.now())
    this.reconnectDelayMs = options.reconnectDelayMs ?? DEFAULT_RECONNECT_DELAY_MS
    this.schedule = options.schedule ?? ((fn, ms) => setTimeout(fn, ms))
    this.clearScheduled =
      options.clearScheduled ??
      ((handle) => clearTimeout(handle as ReturnType<typeof setTimeout>))
    this.autoReconnect = options.autoReconnect ?? true
    this.fetchImpl = options.fetchImpl ?? ((...args) => globalThis.fetch(...args))
  }

  connect(): void {
    this.closedByUs = false
    const socket = this.createSocket(this.url)
    this.socket = socket

    socket.onopen = () => {
      this.connected = true
      this.onConnectionChange?.(true)
      this.reconnectAttempts = 0
      this.lastPongAt = this.now()
      this.scheduleHeartbeat(socket)
      // Fire-and-forget: a resync that fails must not surface as an unhandled
      // rejection. Events are held until the snapshot lands ([8-C]), so nothing
      // is lost while this is in flight.
      void this.resync().catch(() => undefined)
    }
    socket.onmessage = (ev) => this.handleMessage(ev.data)
    socket.onclose = () => {
      // ★ Only the *current* socket's close may drive a reconnect. connect()
      // replaces this.socket but a superseded socket's onclose stays live, so
      // without this guard a flap multiplies: every stale socket that closes
      // schedules another reconnect, and the pile of concurrent reconnects each
      // open-and-immediately-close, never stabilising — so the resync that would
      // backfill a missed event never lands ([8-C] reliability, KI-1).
      if (this.socket !== socket) return
      this.connected = false
      this.onConnectionChange?.(false)
      if (!this.closedByUs && this.autoReconnect) {
        this.reconnectAttempts += 1
        this.reconnectHandle = this.schedule(() => {
          this.reconnectHandle = undefined
          // A close() that raced this timer wins: no zombie reconnect (audit #11).
          if (this.closedByUs) return
          this.connect()
        }, this.reconnectDelayMs)
      }
    }
    socket.onerror = () => {
      /* onclose follows; reconnect is handled there. */
    }
  }

  close(): void {
    this.closedByUs = true
    // ★ Cancel a reconnect queued by a prior unexpected onclose (audit #11): a
    // close() landing inside the reconnect delay window would otherwise let the
    // queued connect() run *after* close — a zombie socket nobody asked for.
    if (this.reconnectHandle !== undefined) {
      this.clearScheduled(this.reconnectHandle)
      this.reconnectHandle = undefined
    }
    this.socket?.close()
    this.socket = null
    this.connected = false
    this.onConnectionChange?.(false)
  }

  /**
   * [8-C] heartbeat loop (KI-1). Self-reschedules via the injected timer so tests
   * drive it deterministically. If no pong has come for HEARTBEAT_DEAD_MS the
   * socket is half-open — the server dropped it but our onclose never fired — so
   * we force-close it. That close *does* fire onclose, which reconnects and
   * resyncs, backfilling everything the dead socket missed. The socket-identity
   * guard stops a superseded (or closed) socket's loop from continuing.
   */
  private scheduleHeartbeat(socket: SocketLike): void {
    this.schedule(() => {
      if (this.socket !== socket || this.closedByUs) return
      if (this.now() - this.lastPongAt > HEARTBEAT_DEAD_MS) {
        socket.close() // half-open escape → onclose → reconnect → resync
        return
      }
      try {
        socket.send(JSON.stringify({ op: 'ping', data: {} }))
      } catch {
        /* a throwing send just means the next dead-check will fire */
      }
      this.scheduleHeartbeat(socket)
    }, HEARTBEAT_INTERVAL_MS)
  }

  /** Inbound [8-C] event → store queue ([7-D] flushes it). */
  handleMessage(raw: unknown): void {
    let event: WSEvent
    try {
      event = typeof raw === 'string' ? (JSON.parse(raw) as WSEvent) : (raw as WSEvent)
    } catch {
      return // A malformed frame must not kill the stream.
    }
    if (!event || typeof event.op !== 'string') return
    if (event.op === 'pong') {
      // [8-C] liveness reply — proves the connection is alive; not a store event.
      this.lastPongAt = this.now()
      return
    }
    if (event.op === 'snapshot.load') {
      // [8-C]/[5-E]: the whole graph was replaced server-side (load_snapshot, or a
      // replace-import via reload_from). Everything held is stale, so full-resync
      // from /graph.json rather than applying this as an incremental event — the
      // live client would otherwise keep rendering the pre-replace graph (audit #10).
      void this.resync().catch(() => undefined)
      return
    }
    useGraphStore.getState().applyServerEvent(event)
  }

  private sendEvent(event: ClientEvent): boolean {
    if (!this.socket || !this.connected) return false
    this.socket.send(JSON.stringify(event))
    return true
  }

  // --- Client → Server ([8-C]) ---

  sendFocusSet(id: string): boolean {
    return this.sendEvent({ op: 'focus.set', data: { id } })
  }

  sendFilterSet(expression: string): boolean {
    return this.sendEvent({ op: 'filter.set', data: { expression } })
  }

  sendLayerToggle(layer: string): boolean {
    return this.sendEvent({ op: 'layer.toggle', data: { layer } })
  }

  sendLayoutSet(algorithm: string): boolean {
    return this.sendEvent({ op: 'layout.set', data: { algorithm } })
  }

  /**
   * [M2e] Undo/redo the last graph mutation for the whole session. The server
   * re-broadcasts the resulting node/edge/finding events, so the view updates
   * through the normal [8-C] path — there is no dedicated reply to handle here.
   */
  sendUndo(): boolean {
    return this.sendEvent({ op: 'undo', data: {} })
  }

  sendRedo(): boolean {
    return this.sendEvent({ op: 'redo', data: {} })
  }

  /**
   * [8-C]: viewport changes are throttled to 250ms. The most recent value is
   * kept so the last position is not lost mid-window — `flushViewUpdate` sends it.
   */
  sendViewUpdate(mode: ViewMode, zoom: number, cameraPos: { x: number; y: number }): boolean {
    const event: ClientEvent = {
      op: 'view.update',
      data: { mode, zoom, camera_pos: cameraPos },
    }
    const now = this.now()
    if (now - this.lastViewUpdateAt < VIEW_UPDATE_THROTTLE_MS) {
      this.pendingViewUpdate = event
      return false
    }
    this.lastViewUpdateAt = now
    this.pendingViewUpdate = null
    return this.sendEvent(event)
  }

  /** Emit the viewport value held back by the throttle, if any. */
  flushViewUpdate(): boolean {
    if (!this.pendingViewUpdate) return false
    const event = this.pendingViewUpdate
    this.pendingViewUpdate = null
    this.lastViewUpdateAt = this.now()
    return this.sendEvent(event)
  }

  /**
   * [8-C] M1 full-snapshot resync.
   *
   * The procedure and its order matter: the WebSocket is already open and
   * buffering, so events that arrive during the fetch are not lost. The snapshot
   * carries the seq it was taken at; anything at or below that is already baked
   * into it and is dropped, and the rest applies on top. That is what makes
   * "이벤트 무손실" ([15]) true across a reconnect.
   */
  async resync(): Promise<void> {
    // Hold arriving events *before* the fetch starts. Anything that lands while
    // it is in flight is newer than the snapshot; applying it now would only see
    // it undone when the older snapshot arrives and replaces the graph.
    const store = useGraphStore.getState()
    store.beginResync()
    try {
      const response = await this.fetchImpl(this.graphUrl, {
        headers: { accept: 'application/json' },
      })
      if (!response.ok) {
        store.endResync()
        return
      }
      const snapshot = (await response.json()) as GraphSnapshot
      // Releases the hold and drains whatever is newer than snapshot.seq.
      store.applySnapshot(snapshot)
    } catch (error) {
      store.endResync()
      throw error
    }
  }
}
