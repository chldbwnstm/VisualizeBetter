/**
 * Completion verification for TASK 7b — App shell ([9-B]).
 */

import { act, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, test, vi } from 'vitest'

// cosmos.gl is WebGL and cytoscape needs real layout; jsdom has neither. The
// wrappers are thin by design — their logic lives in graphAdapter/subgraph, which
// are tested directly. Here we only assert the shell wires them up.
vi.mock('@cosmos.gl/graph', () => ({
  Graph: class {
    // [KI-1] OverviewCanvas gates its first data-apply on these; a real cosmos
    // flips isReady after async WebGL init. The mock is ready from the start.
    isReady = true
    ready = Promise.resolve()
    setPointPositions() {}
    setPointColors() {}
    setPointSizes() {}
    setLinks() {}
    render() {}
    destroy() {}
    fitView() {}
    // Label overlay reads these; selectLabels is tested directly in labels.test.
    trackPointPositionsByIndices() {}
    getTrackedPointPositionsMap() {
      return new Map()
    }
    getZoomLevel() {
      return 1
    }
    spaceToScreenPosition(p: [number, number]) {
      return p
    }
  },
}))
vi.mock('cytoscape', () => {
  const cy = () => ({
    on: () => {},
    elements: () => ({ remove: () => {} }),
    add: () => {},
    layout: () => ({ run: () => {} }),
    fit: () => {},
    destroy: () => {},
  })
  cy.use = () => {}
  return { default: cy }
})

import App from './App'
import { useGraphStore } from './stores/graphStore'
import type { GraphClient } from './ws/client'
import type { GraphBatchData, Node, WSEvent } from './types'

let seq = 0

function node(id: string): Node {
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

function batch(over: Partial<GraphBatchData>): WSEvent {
  return {
    op: 'graph.batch',
    seq: ++seq,
    data: {
      nodes_added: [],
      nodes_updated: [],
      nodes_deleted: [],
      edges_added: [],
      edges_updated: [],
      edges_deleted: [],
      ...over,
    },
  }
}

/** Wrapped in act(): a flush is a store update React must be told about. */
function push(...events: WSEvent[]) {
  act(() => {
    const store = useGraphStore.getState()
    for (const e of events) store.applyServerEvent(e)
    useGraphStore.getState().flushNow()
  })
}

beforeEach(() => {
  seq = 0
  useGraphStore.getState().reset()
})

describe('shell ([9-B])', () => {
  test('mounts the panels this task owns', () => {
    render(<App client={null} />)

    expect(screen.getByTestId('connection-bar')).toBeInTheDocument()
    expect(screen.getByTestId('right-sidebar')).toBeInTheDocument()
    expect(screen.getByTestId('findings-panel')).toBeInTheDocument()
    expect(screen.getByTestId('inspector-empty')).toBeInTheDocument()
  })

  test('the viewport is the real overview canvas now, not a placeholder', () => {
    render(<App client={null} />)

    expect(screen.getByTestId('overview-canvas')).toBeInTheDocument()
    expect(screen.queryByTestId('viewport-placeholder')).not.toBeInTheDocument()
  })

  test('an empty graph invites the AI to push ([7-A])', () => {
    render(<App client={null} />)

    expect(screen.getByTestId('overview-empty')).toBeInTheDocument()
  })

  test('the detail panel stays shut until something is focused ([7-C])', () => {
    render(<App client={null} />)

    expect(screen.queryByTestId('detail-panel')).not.toBeInTheDocument()
  })

  test('ConnectionBar shows live counts ([9-B])', () => {
    render(<App client={null} />)
    expect(screen.getByTestId('node-count')).toHaveTextContent('nodes 0')

    push(batch({ nodes_added: [node('a'), node('b')] }))

    expect(screen.getByTestId('node-count')).toHaveTextContent('nodes 2')
  })

  test('a finding pushed over the wire appears in the panel', () => {
    render(<App client={null} />)

    push({
      op: 'finding.add',
      seq: ++seq,
      data: {
        finding_id: 'f1',
        title: '결제 실패의 핵심 경로',
        body: '',
        node_ids: [],
        confidence: 0.9,
        evidence: [],
        layer: null,
        tags: [],
        created_by: 'claude',
        created_at: 't',
        updated_at: 't',
      },
    })

    expect(screen.getByText('결제 실패의 핵심 경로')).toBeInTheDocument()
  })
})

describe('[7-C] 2분할 모드 ([9-C] viewMode)', () => {
  test('toggling split sets viewMode', async () => {
    render(<App client={null} />)

    await userEvent.click(screen.getByLabelText('split view'))
    expect(useGraphStore.getState().viewMode).toBe('split')

    await userEvent.click(screen.getByLabelText('split view'))
    expect(useGraphStore.getState().viewMode).toBe('overview')
  })

  test('split shows overview and detail together once a node is focused', async () => {
    push(batch({ nodes_added: [node('a')] }))
    render(<App client={null} />)
    expect(screen.queryByTestId('detail-panel')).not.toBeInTheDocument()

    await userEvent.click(screen.getByLabelText('split view'))
    act(() => useGraphStore.getState().setFocus('a'))

    expect(screen.getByTestId('overview-canvas')).toBeInTheDocument()
    expect(screen.getByTestId('detail-panel')).toBeInTheDocument()
  })

  test('split with nothing focused still shows only the overview', async () => {
    render(<App client={null} />)

    await userEvent.click(screen.getByLabelText('split view'))

    expect(screen.queryByTestId('detail-panel')).not.toBeInTheDocument()
  })

  test('closing the detail while split leaves split mode', async () => {
    push(batch({ nodes_added: [node('a')] }))
    render(<App client={null} />)
    await userEvent.click(screen.getByLabelText('split view'))
    act(() => useGraphStore.getState().setFocus('a'))

    await userEvent.click(screen.getByTestId('detail-close'))

    expect(useGraphStore.getState().viewMode).toBe('overview')
    expect(screen.queryByTestId('detail-panel')).not.toBeInTheDocument()
  })
})

describe('[M2e] undo/redo controls', () => {
  function fakeClient() {
    return {
      connect: vi.fn(),
      close: vi.fn(),
      onConnectionChange: undefined as ((c: boolean) => void) | undefined,
      sendUndo: vi.fn(() => true),
      sendRedo: vi.fn(() => true),
      sendFocusSet: vi.fn(() => true),
      sendFilterSet: vi.fn(() => true),
    } as unknown as GraphClient
  }

  test('the toolbar shows undo and redo buttons', () => {
    render(<App client={null} />)

    expect(screen.getByTestId('undo-button')).toBeInTheDocument()
    expect(screen.getByTestId('redo-button')).toBeInTheDocument()
  })

  test('clicking undo/redo sends the op to the server ([8-C] round-trip)', async () => {
    const client = fakeClient()
    render(<App client={client} />)

    await userEvent.click(screen.getByTestId('undo-button'))
    expect(client.sendUndo).toHaveBeenCalledTimes(1)

    await userEvent.click(screen.getByTestId('redo-button'))
    expect(client.sendRedo).toHaveBeenCalledTimes(1)
  })
})

describe('[5-D] AI → screen', () => {
  test('a server focus.set opens the detail view (AI focus_on)', () => {
    push(batch({ nodes_added: [node('a')] }))
    render(<App client={null} />)
    expect(screen.queryByTestId('detail-panel')).not.toBeInTheDocument()

    push({ op: 'focus.set', seq: ++seq, data: { id: 'a' } } as WSEvent)

    expect(screen.getByTestId('detail-panel')).toBeInTheDocument()
  })

  test('a filter.suggest shows the suggestion banner in the shell', () => {
    render(<App client={null} />)
    expect(screen.queryByTestId('suggestion-banner')).not.toBeInTheDocument()

    push({ op: 'filter.suggest', seq: ++seq, data: { expression: 'type == "class"', reason: 'r' } } as WSEvent)

    expect(screen.getByTestId('suggestion-banner')).toBeInTheDocument()
    expect(screen.getByTestId('suggestion-expression')).toHaveTextContent('type == "class"')
  })
})
