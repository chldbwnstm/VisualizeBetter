/**
 * Completion verification for TASK 7a — WS client routing ([8-C]).
 */

import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest'
import {
  GraphClient,
  HEARTBEAT_DEAD_MS,
  HEARTBEAT_INTERVAL_MS,
  VIEW_UPDATE_THROTTLE_MS,
  type SocketLike,
} from './client'
import { graphData, useGraphStore } from '../stores/graphStore'
// eslint-disable-next-line @typescript-eslint/no-unused-vars -- used via graphData

class FakeSocket implements SocketLike {
  sent: string[] = []
  closed = false
  onopen: ((ev: unknown) => unknown) | null = null
  onclose: ((ev: unknown) => unknown) | null = null
  onmessage: ((ev: { data: unknown }) => unknown) | null = null
  onerror: ((ev: unknown) => unknown) | null = null

  send(data: string) {
    this.sent.push(data)
  }

  close() {
    this.closed = true
    this.onclose?.(null)
  }

  open() {
    this.onopen?.(null)
  }

  receive(payload: unknown) {
    this.onmessage?.({ data: JSON.stringify(payload) })
  }

  get parsed() {
    return this.sent.map((s) => JSON.parse(s))
  }
}

function makeClient(over: Partial<ConstructorParameters<typeof GraphClient>[0]> = {}) {
  const sockets: FakeSocket[] = []
  const client = new GraphClient({
    url: 'ws://localhost:8765/live',
    createSocket: () => {
      const s = new FakeSocket()
      sockets.push(s)
      return s
    },
    // Default stub so connect() never reaches the network from a unit test.
    fetchImpl: async () => ({ ok: false }) as unknown as Response,
    ...over,
  })
  return { client, sockets }
}

beforeEach(() => {
  useGraphStore.getState().reset()
})

/**
 * Let the resync that connect() kicks off settle.
 *
 * Opening the socket starts a /graph.json fetch, and events are held until it
 * lands ([8-C]). Without waiting, a test's own pushes sit in the buffer and it
 * looks like routing is broken.
 */
async function settled() {
  await new Promise((resolve) => setTimeout(resolve, 0))
}

describe('connection lifecycle', () => {
  test('connect opens a socket and marks connected', async () => {
    const { client, sockets } = makeClient()
    client.connect()
    sockets[0].open()

    expect(client.connected).toBe(true)
    await settled()
  })

  test('close does not reconnect', () => {
    const scheduled: Array<() => void> = []
    const { client, sockets } = makeClient({ schedule: (fn) => void scheduled.push(fn) })
    client.connect()
    sockets[0].open() // arms the [8-C] heartbeat timer via schedule()

    client.close()
    // Draining every armed timer (the heartbeat included) must not open a new
    // socket: a user close is not a drop, so nothing reconnects.
    scheduled.forEach((fn) => fn())

    expect(client.connected).toBe(false)
    expect(sockets).toHaveLength(1)
  })

  test('an unexpected drop schedules a reconnect', async () => {
    const scheduled: Array<() => void> = []
    const { client, sockets } = makeClient({ schedule: (fn) => void scheduled.push(fn) })
    client.connect()
    sockets[0].open()
    await settled()

    sockets[0].onclose?.(null)

    expect(client.connected).toBe(false)
    expect(client.reconnectAttempts).toBe(1)

    // open() armed the heartbeat first; the drop appended the reconnect timer.
    scheduled[scheduled.length - 1]()
    sockets[1].open()
    await settled()
    expect(client.connected).toBe(true)
    expect(client.reconnectAttempts).toBe(0)
  })
})

describe('inbound routing → store', () => {
  test('graph.batch reaches the store through the flush', async () => {
    const { client, sockets } = makeClient()
    client.connect()
    sockets[0].open()
    await settled()

    sockets[0].receive({
      op: 'graph.batch',
      seq: 1,
      data: {
        nodes_added: [
          {
            id: 'a',
            label: 'A',
            type: 'class',
            properties: {},
            parent_id: null,
            style_hint: null,
            position_hint: null,
            layer: null,
            ttl: 0,
            tags: [],
            created_at: 't',
            updated_at: 't',
            created_by: null,
          },
        ],
        nodes_updated: [],
        nodes_deleted: [],
        edges_added: [],
        edges_updated: [],
        edges_deleted: [],
      },
    })
    useGraphStore.getState().flushNow()

    expect(graphData.getNode('a')?.label).toBe('A')
  })

  test('finding.add reaches the store', async () => {
    const { client, sockets } = makeClient()
    client.connect()
    sockets[0].open()
    await settled()

    sockets[0].receive({
      op: 'finding.add',
      seq: 1,
      data: {
        finding_id: 'f1',
        title: 'gold',
        body: '',
        node_ids: [],
        confidence: 0.9,
        evidence: [],
        layer: null,
        tags: [],
        created_by: null,
        created_at: 't',
        updated_at: 't',
      },
    })
    useGraphStore.getState().flushNow()

    expect(useGraphStore.getState().findings.get('f1')?.confidence).toBe(0.9)
  })

  test('focus.set reaches the store', async () => {
    const { client, sockets } = makeClient()
    client.connect()
    sockets[0].open()
    await settled()

    sockets[0].receive({ op: 'focus.set', seq: 1, data: { id: 'a' } })
    useGraphStore.getState().flushNow()

    expect(useGraphStore.getState().focus).toBe('a')
  })

  test('a malformed frame does not throw', () => {
    const { client, sockets } = makeClient()
    client.connect()
    sockets[0].open()

    expect(() => sockets[0].onmessage?.({ data: 'not json' })).not.toThrow()
    expect(() => sockets[0].onmessage?.({ data: '{"no":"op"}' })).not.toThrow()
  })
})

describe('outbound [8-C] Client → Server', () => {
  test('focus.set / filter.set / layer.toggle / layout.set', async () => {
    const { client, sockets } = makeClient()
    client.connect()
    sockets[0].open()
    await settled()

    client.sendFocusSet('a')
    client.sendFilterSet('type == "class"')
    client.sendLayerToggle('l1')
    client.sendLayoutSet('dagre')

    expect(sockets[0].parsed).toEqual([
      { op: 'focus.set', data: { id: 'a' } },
      { op: 'filter.set', data: { expression: 'type == "class"' } },
      { op: 'layer.toggle', data: { layer: 'l1' } },
      { op: 'layout.set', data: { algorithm: 'dagre' } },
    ])
  })

  test('sending while disconnected is a no-op, not a crash', () => {
    const { client } = makeClient()

    expect(client.sendFocusSet('a')).toBe(false)
  })

  test('[M2e] undo / redo send the bare [8-C] op', async () => {
    const { client, sockets } = makeClient()
    client.connect()
    sockets[0].open()
    await settled()

    client.sendUndo()
    client.sendRedo()

    expect(sockets[0].parsed).toEqual([
      { op: 'undo', data: {} },
      { op: 'redo', data: {} },
    ])
  })
})

describe('view.update throttle ([8-C] 250ms)', () => {
  test('the throttle window is 250ms', () => {
    expect(VIEW_UPDATE_THROTTLE_MS).toBe(250)
  })

  test('bursts inside the window collapse to one send', async () => {
    let clock = 1000
    const { client, sockets } = makeClient({ now: () => clock })
    client.connect()
    sockets[0].open()
    await settled()

    expect(client.sendViewUpdate('overview', 1, { x: 0, y: 0 })).toBe(true)
    clock += 50
    expect(client.sendViewUpdate('overview', 1.1, { x: 1, y: 0 })).toBe(false)
    clock += 50
    expect(client.sendViewUpdate('overview', 1.2, { x: 2, y: 0 })).toBe(false)

    expect(sockets[0].sent).toHaveLength(1)
  })

  test('a send past the window goes out', async () => {
    let clock = 1000
    const { client, sockets } = makeClient({ now: () => clock })
    client.connect()
    sockets[0].open()
    await settled()

    client.sendViewUpdate('overview', 1, { x: 0, y: 0 })
    clock += VIEW_UPDATE_THROTTLE_MS + 1
    client.sendViewUpdate('detail', 2, { x: 9, y: 9 })

    expect(sockets[0].parsed).toHaveLength(2)
    expect(sockets[0].parsed[1].data).toEqual({
      mode: 'detail',
      zoom: 2,
      camera_pos: { x: 9, y: 9 },
    })
  })

  test('the last throttled value is not lost', async () => {
    let clock = 1000
    const { client, sockets } = makeClient({ now: () => clock })
    client.connect()
    sockets[0].open()
    await settled()

    client.sendViewUpdate('overview', 1, { x: 0, y: 0 })
    clock += 10
    client.sendViewUpdate('overview', 5, { x: 7, y: 7 })

    expect(client.flushViewUpdate()).toBe(true)
    expect(sockets[0].parsed[1].data.zoom).toBe(5)
  })

  test('flushing with nothing held back sends nothing', async () => {
    const { client, sockets } = makeClient()
    client.connect()
    sockets[0].open()
    await settled()

    expect(client.flushViewUpdate()).toBe(false)
    expect(sockets[0].sent).toHaveLength(0)
  })

  test('view.update payload uses the wire shape (camera_pos)', async () => {
    const { client, sockets } = makeClient()
    client.connect()
    sockets[0].open()
    await settled()

    client.sendViewUpdate('split', 1.5, { x: 3, y: -4 })

    expect(sockets[0].parsed[0]).toEqual({
      op: 'view.update',
      data: { mode: 'split', zoom: 1.5, camera_pos: { x: 3, y: -4 } },
    })
  })
})

describe('resync ([8-C] M1 full-snapshot)', () => {
  function snapshotResponse(body: unknown) {
    return { ok: true, json: async () => body } as unknown as Response
  }

  function node(id: string) {
    return {
      id,
      label: id.toUpperCase(),
      type: 'class',
      properties: {},
      parent_id: null,
      style_hint: null,
      position_hint: null,
      layer: null,
      ttl: 0,
      tags: [],
      created_at: 't',
      updated_at: 't',
      created_by: null,
    }
  }

  test('derives /graph.json from the ws url', () => {
    const { client } = makeClient()
    expect(client.graphUrl).toBe('http://localhost:8765/graph.json')
  })

  test('loads the snapshot into the store', async () => {
    const { client } = makeClient({
      fetchImpl: async () =>
        snapshotResponse({ seq: 7, nodes: [node('a')], edges: [], findings: [] }),
    })

    await client.resync()

    expect(graphData.getNode('a')).toBeDefined()
    expect(useGraphStore.getState().seq).toBe(7)
  })

  test('connecting resyncs automatically', async () => {
    const { client, sockets } = makeClient({
      fetchImpl: async () =>
        snapshotResponse({ seq: 1, nodes: [node('boot')], edges: [], findings: [] }),
    })
    client.connect()
    sockets[0].open()
    await new Promise((r) => setTimeout(r, 0))

    expect(graphData.getNode('boot')).toBeDefined()
  })

  test('events already inside the snapshot are dropped ([8-C])', async () => {
    const { client } = makeClient({
      fetchImpl: async () =>
        snapshotResponse({ seq: 5, nodes: [node('a')], edges: [], findings: [] }),
    })
    // Buffered while the fetch was in flight: seq 3 is already baked into the
    // snapshot, seq 9 is not.
    useGraphStore.getState().applyServerEvent({ op: 'node.delete', data: { id: 'a' }, seq: 3 })
    useGraphStore.getState().applyServerEvent({ op: 'focus.set', data: { id: 'a' }, seq: 9 })

    await client.resync()
    useGraphStore.getState().flushNow()

    expect(graphData.getNode('a')).toBeDefined(), 'the stale delete was discarded'
    expect(useGraphStore.getState().focus).toBe('a'), 'the newer event still applied'
  })

  test('★ an event arriving during the fetch is not undone by the snapshot', async () => {
    // [8-C] M1: the WS connects and buffers *while* /graph.json is fetched. The
    // snapshot predates anything that lands in that window, so applying those
    // events eagerly means the snapshot wipes them when it arrives.
    let release: (value: Response) => void = () => {}
    const pending = new Promise<Response>((resolve) => {
      release = resolve
    })
    const { client } = makeClient({ fetchImpl: () => pending })

    const resyncing = client.resync()
    // Arrives mid-fetch, and is newer than the snapshot's seq. Even a flush
    // attempt must not apply it yet.
    useGraphStore.getState().applyServerEvent({ op: 'node.add', data: node('live'), seq: 9 })
    useGraphStore.getState().flushNow()
    expect(graphData.getNode('live')).toBeUndefined(), 'held until the snapshot lands'

    release(snapshotResponse({ seq: 5, nodes: [node('fromSnapshot')], edges: [], findings: [] }))
    await resyncing
    useGraphStore.getState().flushNow()

    expect(graphData.getNode('fromSnapshot')).toBeDefined()
    expect(graphData.getNode('live')).toBeDefined(), 'the live push survived the resync'
    expect(useGraphStore.getState().seq).toBe(9)
  })

  test('a failed fetch releases the hold so events flow again', async () => {
    const { client } = makeClient({
      fetchImpl: async () => ({ ok: false }) as unknown as Response,
    })

    await client.resync()
    useGraphStore.getState().applyServerEvent({ op: 'node.add', data: node('after'), seq: 1 })
    useGraphStore.getState().flushNow()

    expect(graphData.getNode('after')).toBeDefined()
  })

  test('a thrown fetch releases the hold', async () => {
    const { client } = makeClient({
      fetchImpl: async () => {
        throw new Error('network down')
      },
    })

    await expect(client.resync()).rejects.toThrow('network down')
    useGraphStore.getState().applyServerEvent({ op: 'node.add', data: node('after'), seq: 1 })
    useGraphStore.getState().flushNow()

    expect(graphData.getNode('after')).toBeDefined()
  })

  test('a failed fetch leaves the store alone', async () => {
    const { client } = makeClient({
      fetchImpl: async () => ({ ok: false } as unknown as Response),
    })

    await expect(client.resync()).resolves.toBeUndefined()
    expect(useGraphStore.getState().seq).toBe(0)
  })

  test('the snapshot replaces rather than merges', async () => {
    useGraphStore.getState().applyServerEvent({ op: 'node.add', data: node('stale'), seq: 1 })
    useGraphStore.getState().flushNow()

    const { client } = makeClient({
      fetchImpl: async () =>
        snapshotResponse({ seq: 9, nodes: [node('fresh')], edges: [], findings: [] }),
    })
    await client.resync()

    expect(graphData.getNode('stale')).toBeUndefined()
    expect(graphData.getNode('fresh')).toBeDefined()
  })
})

describe('★ [8-C] heartbeat / half-open recovery (KI-1)', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => vi.useRealTimers())

  const okSnapshot = async () =>
    ({
      ok: true,
      json: async () => ({ seq: 0, nodes: [], edges: [], findings: [] }),
    }) as unknown as Response

  test('pings after the interval', async () => {
    const { client, sockets } = makeClient({ fetchImpl: okSnapshot })
    client.connect()
    sockets[0].open()

    await vi.advanceTimersByTimeAsync(HEARTBEAT_INTERVAL_MS + 1)

    expect(sockets[0].parsed.filter((m) => m.op === 'ping')).not.toHaveLength(0)
  })

  test('a pong keeps the socket alive — no reconnect', async () => {
    const { client, sockets } = makeClient({ fetchImpl: okSnapshot })
    client.connect()
    sockets[0].open()

    for (let i = 0; i < 6; i += 1) {
      await vi.advanceTimersByTimeAsync(HEARTBEAT_INTERVAL_MS + 1)
      sockets[0].receive({ op: 'pong', data: {}, seq: 1 }) // server is alive
    }

    expect(sockets).toHaveLength(1)
    expect(sockets[0].closed).toBe(false)
  })

  test('★ no pong for HEARTBEAT_DEAD_MS force-closes the half-open socket and reconnects', async () => {
    const { client, sockets } = makeClient({ fetchImpl: okSnapshot })
    client.connect()
    sockets[0].open()

    // Never answer a ping — the server dropped us but our onclose never fired.
    await vi.advanceTimersByTimeAsync(HEARTBEAT_DEAD_MS + HEARTBEAT_INTERVAL_MS + 1)
    expect(sockets[0].closed).toBe(true) // force-closed → onclose → reconnect scheduled
    await vi.advanceTimersByTimeAsync(1100) // reconnect delay

    expect(sockets.length).toBeGreaterThan(1) // a fresh socket was opened
  })

  test('★ the reconnect resyncs, backfilling what the dead socket missed', async () => {
    const fetchSpy = vi.fn(okSnapshot)
    const { client, sockets } = makeClient({ fetchImpl: fetchSpy })
    client.connect()
    sockets[0].open()
    await vi.advanceTimersByTimeAsync(1) // initial resync
    const initialFetches = fetchSpy.mock.calls.length

    await vi.advanceTimersByTimeAsync(HEARTBEAT_DEAD_MS + HEARTBEAT_INTERVAL_MS + 1)
    await vi.advanceTimersByTimeAsync(1100) // reconnect
    sockets[sockets.length - 1].open() // the fresh socket opens → resync again
    await vi.advanceTimersByTimeAsync(1)

    // A second /graph.json fetch on reconnect is the backfill ([8-C] resync).
    expect(fetchSpy.mock.calls.length).toBeGreaterThan(initialFetches)
  })

  test('a pong is not routed to the store as an event', async () => {
    const { client, sockets } = makeClient({ fetchImpl: okSnapshot })
    client.connect()
    sockets[0].open()
    const before = useGraphStore.getState().seq

    sockets[0].receive({ op: 'pong', data: {}, seq: 999 })

    // pong updates liveness only; it must not bump the store seq.
    expect(useGraphStore.getState().seq).toBe(before)
  })
})

describe('★ Z5 audit fixes', () => {
  test('snapshot.load triggers a resync — live full-replace recovery (audit #10)', async () => {
    const fetchSpy = vi.fn(
      async () =>
        ({
          ok: true,
          json: async () => ({ seq: 5, nodes: [], edges: [], findings: [] }),
        }) as unknown as Response,
    )
    const { client, sockets } = makeClient({ fetchImpl: fetchSpy })
    client.connect()
    sockets[0].open()
    await settled() // the onopen resync settles first
    const before = fetchSpy.mock.calls.length

    sockets[0].receive({ op: 'snapshot.load', seq: 6, data: { snapshot_id: 's1' } })
    await settled()

    // ★ snapshot.load must re-fetch /graph.json; routing it to the store instead
    // (the audit #10 regression) leaves this unchanged.
    expect(fetchSpy.mock.calls.length).toBeGreaterThan(before)
    expect(fetchSpy).toHaveBeenLastCalledWith(client.graphUrl, expect.anything())
  })

  test('close() cancels a pending reconnect — no zombie socket (audit #11)', () => {
    const scheduled: Array<{ fn: () => void; handle: number }> = []
    const cleared: number[] = []
    let next = 1
    const { client, sockets } = makeClient({
      schedule: (fn) => {
        const handle = next++
        scheduled.push({ fn, handle })
        return handle
      },
      clearScheduled: (handle) => void cleared.push(handle as number),
    })
    client.connect()
    sockets[0].open()

    sockets[0].onclose?.(null) // an unexpected drop schedules a reconnect
    const reconnect = scheduled[scheduled.length - 1]
    client.close() // the caller closes before the reconnect fires

    expect(cleared).toContain(reconnect.handle) // ★ the queued timer was cancelled
    reconnect.fn() // even if the stale timer somehow fires...
    expect(sockets).toHaveLength(1) // ...no zombie reconnect
    expect(client.connected).toBe(false)
  })
})
