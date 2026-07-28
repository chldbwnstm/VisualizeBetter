/**
 * Completion verification for TASK 7b — NodeInspector ([9-B], [23-B], [11]).
 */

import { act, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, test, vi } from 'vitest'
import { NodeInspector } from './NodeInspector'
import { useGraphStore } from '../stores/graphStore'
import type { Edge, GraphBatchData, Node, WSEvent } from '../types'

let seq = 0

function node(id: string, over: Partial<Node> = {}): Node {
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
    ...over,
  }
}

function edge(source: string, target: string, over: Partial<Edge> = {}): Edge {
  return {
    source,
    target,
    relation: 'field',
    key: '',
    directed: true,
    properties: {},
    weight: 1,
    layer: null,
    style_hint: null,
    ttl: 0,
    tags: [],
    created_at: 't',
    created_by: null,
    ...over,
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

describe('empty / missing states', () => {
  test('nothing focused', () => {
    render(<NodeInspector />)
    expect(screen.getByTestId('inspector-empty')).toBeInTheDocument()
  })

  test('focused id not yet loaded', () => {
    useGraphStore.getState().setFocus('ghost')
    render(<NodeInspector />)
    expect(screen.getByTestId('inspector-missing')).toBeInTheDocument()
  })
})

describe('properties table ([9-B])', () => {
  test('renders the focused node header and properties', () => {
    push(batch({ nodes_added: [node('a', { properties: { ns: 'app.ui', count: 3 }, layer: 'l1' })] }))
    useGraphStore.getState().setFocus('a')

    render(<NodeInspector />)

    expect(screen.getByTestId('inspector-label')).toHaveTextContent('A')
    expect(screen.getByTestId('inspector-type')).toHaveTextContent('class')
    expect(screen.getByTestId('inspector-layer')).toHaveTextContent('l1')
    const rows = screen.getAllByTestId('property-row')
    expect(rows).toHaveLength(2)
    expect(rows[0]).toHaveTextContent('ns')
    expect(rows[0]).toHaveTextContent('app.ui')
  })

  test('non-string values render readably', () => {
    push(batch({ nodes_added: [node('a', { properties: { flag: true, n: 3, obj: { x: 1 } } })] }))
    useGraphStore.getState().setFocus('a')

    render(<NodeInspector />)

    expect(screen.getByTestId('properties-table')).toHaveTextContent('true')
    expect(screen.getByTestId('properties-table')).toHaveTextContent('{"x":1}')
  })

  test('reserved keys are hidden from the table ([23-B])', () => {
    push(
      batch({
        nodes_added: [
          node('a', {
            properties: {
              ns: 'MOD',
              _citations: [{ url: 'https://example.test/d', title: 'Doc', ts: 't' }],
              _internal: 'secret',
            },
          }),
        ],
      }),
    )
    useGraphStore.getState().setFocus('a')

    render(<NodeInspector />)

    const table = screen.getByTestId('properties-table')
    expect(table).toHaveTextContent('ns')
    expect(table).not.toHaveTextContent('_citations')
    expect(table).not.toHaveTextContent('_internal')
  })

  test('empty properties', () => {
    push(batch({ nodes_added: [node('a')] }))
    useGraphStore.getState().setFocus('a')

    render(<NodeInspector />)

    expect(screen.getByTestId('properties-empty')).toBeInTheDocument()
  })
})

describe('citations section ([23-B], [11])', () => {
  test('renders _citations with title and url', () => {
    push(
      batch({
        nodes_added: [
          node('a', {
            properties: {
              _citations: [
                { url: 'https://example.test/doc', title: 'Spec doc', ts: 't' },
                { url: 'trace://0x1400', title: 'Trace: sub_1400', ts: 't' },
              ],
            },
          }),
        ],
      }),
    )
    useGraphStore.getState().setFocus('a')

    render(<NodeInspector />)

    const items = screen.getAllByTestId('citation-item')
    expect(items).toHaveLength(2)
    expect(items[0]).toHaveTextContent('Spec doc')
    expect(items[1]).toHaveTextContent('Trace: sub_1400')
  })

  test('http citation is a link, non-http is plain text ([11] allowlist)', () => {
    push(
      batch({
        nodes_added: [
          node('a', {
            properties: {
              _citations: [
                { url: 'https://example.test/doc', title: 'Doc', ts: 't' },
                { url: 'trace://0x1400', title: 'IDA', ts: 't' },
              ],
            },
          }),
        ],
      }),
    )
    useGraphStore.getState().setFocus('a')

    render(<NodeInspector />)

    const links = screen.getAllByRole('link')
    expect(links).toHaveLength(1)
    expect(links[0]).toHaveAttribute('href', 'https://example.test/doc')
    expect(screen.getByText('trace://0x1400').tagName).toBe('SPAN')
  })

  test.each([
    ['javascript:alert(1)'],
    ['data:text/html,<script>alert(1)</script>'],
    ['file:///etc/passwd'],
  ])('a hostile citation url %s never becomes an anchor', (url) => {
    push(
      batch({
        nodes_added: [node('a', { properties: { _citations: [{ url, title: 'evil', ts: 't' }] } })],
      }),
    )
    useGraphStore.getState().setFocus('a')

    render(<NodeInspector />)

    expect(screen.queryByRole('link')).not.toBeInTheDocument()
    expect(screen.getByTestId('citation-url')).toHaveTextContent(url)
  })

  test('a script tag in a citation title renders as text', () => {
    push(
      batch({
        nodes_added: [
          node('a', {
            properties: {
              _citations: [{ url: 'https://x.test', title: '<script>alert(1)</script>', ts: 't' }],
            },
          }),
        ],
      }),
    )
    useGraphStore.getState().setFocus('a')

    const { container } = render(<NodeInspector />)

    expect(screen.getByTestId('citation-title')).toHaveTextContent('<script>alert(1)</script>')
    expect(container.querySelector('script')).toBeNull()
  })

  test('no citations section when the node has none', () => {
    push(batch({ nodes_added: [node('a')] }))
    useGraphStore.getState().setFocus('a')

    render(<NodeInspector />)

    expect(screen.queryByTestId('citations-section')).not.toBeInTheDocument()
  })
})

describe('neighbors list ([9-B])', () => {
  test('lists both incoming and outgoing neighbours', () => {
    push(
      batch({
        nodes_added: [node('a'), node('b'), node('c')],
        edges_added: [edge('a', 'b'), edge('c', 'a', { relation: 'call' })],
      }),
    )
    useGraphStore.getState().setFocus('a')

    render(<NodeInspector />)

    const list = screen.getByTestId('neighbors-list')
    expect(list).toHaveTextContent('이웃 (2)')
    expect(within(list).getByText('b')).toBeInTheDocument()
    expect(within(list).getByText('c')).toBeInTheDocument()
  })

  test('clicking a neighbour refocuses and calls the seam', async () => {
    push(batch({ nodes_added: [node('a'), node('b')], edges_added: [edge('a', 'b')] }))
    useGraphStore.getState().setFocus('a')
    const onSelectNeighbor = vi.fn()

    render(<NodeInspector onSelectNeighbor={onSelectNeighbor} />)
    await userEvent.click(screen.getByTestId('neighbor-item'))

    expect(useGraphStore.getState().focus).toBe('b')
    expect(onSelectNeighbor).toHaveBeenCalledWith('b')
  })

  test('parallel edges each list a neighbour entry', () => {
    push(
      batch({
        nodes_added: [node('a'), node('b')],
        edges_added: [edge('a', 'b', { key: 'm_health' }), edge('a', 'b', { key: 'm_mana' })],
      }),
    )
    useGraphStore.getState().setFocus('a')

    render(<NodeInspector />)

    expect(screen.getAllByTestId('neighbor-item')).toHaveLength(2)
  })

  test('an isolated node has no neighbours', () => {
    push(batch({ nodes_added: [node('a')] }))
    useGraphStore.getState().setFocus('a')

    render(<NodeInspector />)

    expect(screen.getByTestId('neighbors-list')).toHaveTextContent('이웃 (0)')
  })
})

describe('[7-D] off-React reads', () => {
  test('a later flush is reflected via the seq subscription', () => {
    push(batch({ nodes_added: [node('a', { properties: { ns: 'before' } })] }))
    useGraphStore.getState().setFocus('a')
    render(<NodeInspector />)
    expect(screen.getByTestId('properties-table')).toHaveTextContent('before')

    push(batch({ nodes_updated: [{ id: 'a', patch: { set: { properties: { ns: 'after' } } } }] }))

    expect(screen.getByTestId('properties-table')).toHaveTextContent('after')
  })
})

describe('[24] 이력 / 변경로그', () => {
  const superseded = [{ prev: { label: 'OldName' }, at: '2026-01-01T00:00:00Z', by: 'ida-agent' }]
  const provenance = [{ action: 'correction', at: '2026-01-02T00:00:00Z', by: null }]

  test('neither section renders when there is no history', () => {
    push(batch({ nodes_added: [node('a')] }))
    useGraphStore.getState().setFocus('a')

    render(<NodeInspector />)

    expect(screen.queryByTestId('superseded-section')).not.toBeInTheDocument()
    expect(screen.queryByTestId('provenance-section')).not.toBeInTheDocument()
  })

  test('[24-C] the superseded value is shown', () => {
    push(batch({ nodes_added: [node('a', { properties: { _superseded: superseded } })] }))
    useGraphStore.getState().setFocus('a')

    render(<NodeInspector />)

    const section = screen.getByTestId('superseded-section')
    expect(section).toHaveTextContent('이력 (superseded, 1)')
    expect(section).toHaveTextContent('OldName')
    expect(section).toHaveTextContent('ida-agent')
  })

  test('[24-B] the correction log is shown', () => {
    push(batch({ nodes_added: [node('a', { properties: { _provenance: provenance } })] }))
    useGraphStore.getState().setFocus('a')

    render(<NodeInspector />)

    const section = screen.getByTestId('provenance-section')
    expect(section).toHaveTextContent('변경로그 (provenance, 1)')
    expect(screen.getByTestId('provenance-action')).toHaveTextContent('correction')
  })

  test('[23-B] history stays out of the properties table', () => {
    push(
      batch({
        nodes_added: [node('a', { properties: { _superseded: superseded, real: 'shown' } })],
      }),
    )
    useGraphStore.getState().setFocus('a')

    render(<NodeInspector />)

    const table = screen.getByTestId('properties-table')
    expect(table).toHaveTextContent('shown')
    expect(table).not.toHaveTextContent('OldName')
  })

  test('history arriving by [8-C] node.update reaches the inspector', () => {
    push(batch({ nodes_added: [node('a')] }))
    useGraphStore.getState().setFocus('a')
    render(<NodeInspector />)
    expect(screen.queryByTestId('superseded-section')).not.toBeInTheDocument()

    // The server writes the archive, so the patch it publishes carries it —
    // otherwise the inspector would show nothing until a full resync.
    push(
      batch({
        nodes_updated: [
          { id: 'a', patch: { set: { label: 'NewName', properties: { _superseded: superseded } } } },
        ],
      }),
    )

    expect(screen.getByTestId('superseded-section')).toHaveTextContent('OldName')
  })

  test('[11] an archived value renders as text, never as markup', () => {
    const hostile = [
      { prev: { label: '<img src=x onerror="alert(1)">' }, at: 't', by: '<script>bad()</script>' },
    ]
    push(batch({ nodes_added: [node('a', { properties: { _superseded: hostile } })] }))
    useGraphStore.getState().setFocus('a')

    const { container } = render(<NodeInspector />)

    // The archive is the one place the UI shows values the AI already replaced;
    // markup smuggled in before a correction must not still be live here.
    expect(container.querySelector('img')).toBeNull()
    expect(container.querySelector('script')).toBeNull()
    expect(screen.getByTestId('superseded-value')).toHaveTextContent(
      '<img src=x onerror="alert(1)">',
    )
  })

  test('a non-string archived value is stringified, not dropped', () => {
    const entries = [{ prev: { properties: { size: 64 }, weight: 1.5 }, at: 't', by: null }]
    push(batch({ nodes_added: [node('a', { properties: { _superseded: entries } })] }))
    useGraphStore.getState().setFocus('a')

    render(<NodeInspector />)

    expect(screen.getByTestId('superseded-section')).toHaveTextContent('64')
  })
})
