/**
 * Completion verification for TASK 7b — FindingsPanel ([23-F] TASK 7, [11]).
 */

import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, test, vi } from 'vitest'
import { FindingsPanel } from './FindingsPanel'
import { useGraphStore } from '../stores/graphStore'
import type { Finding, Node, WSEvent } from '../types'

let seq = 0

function finding(id: string, over: Partial<Finding> = {}): Finding {
  return {
    finding_id: id,
    title: `finding ${id}`,
    body: '',
    node_ids: [],
    confidence: 0.8,
    evidence: [],
    layer: null,
    tags: [],
    created_by: null,
    created_at: `2026-07-1${(seq % 9) + 1}T00:00:00+00:00`,
    updated_at: '2026-07-17T00:00:00+00:00',
    ...over,
  }
}

function node(id: string, over: Partial<Node> = {}): Node {
  return {
    id,
    label: id,
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

function push(...events: WSEvent[]) {
  const store = useGraphStore.getState()
  for (const e of events) store.applyServerEvent(e)
  useGraphStore.getState().flushNow()
}

function addFinding(f: Finding) {
  push({ op: 'finding.add', data: f, seq: ++seq })
}

beforeEach(() => {
  seq = 0
  useGraphStore.getState().reset()
})

describe('findings list ([23-F] TASK 7)', () => {
  test('empty state', () => {
    render(<FindingsPanel />)
    expect(screen.getByTestId('findings-empty')).toBeInTheDocument()
  })

  test('renders title, confidence and created_by', () => {
    addFinding(finding('f1', { title: '결제 실패의 핵심 경로', confidence: 0.95, created_by: 'claude' }))

    render(<FindingsPanel />)

    expect(screen.getByText('결제 실패의 핵심 경로')).toBeInTheDocument()
    expect(screen.getByTestId('finding-confidence')).toHaveTextContent('0.95')
    expect(screen.getByTestId('finding-author')).toHaveTextContent('claude')
  })

  test('shows a layer colour dot, hashed when unassigned ([10-A])', () => {
    addFinding(finding('f1', { layer: 'claude-1' }))

    render(<FindingsPanel />)

    const dot = screen.getByTestId('layer-dot')
    expect(dot.getAttribute('data-color')).toMatch(/^#[0-9a-f]{6}$/)
  })

  test('the same layer always gets the same colour', () => {
    // Determinism is the guarantee; distinctness is not — a fixed palette must
    // collide eventually, so asserting different layers differ would be false.
    addFinding(finding('f1', { layer: 'claude-1' }))
    addFinding(finding('f2', { layer: 'claude-1' }))

    render(<FindingsPanel />)

    const [a, b] = screen.getAllByTestId('layer-dot').map((d) => d.getAttribute('data-color'))
    expect(a).toBe(b)
    expect(a).toMatch(/^#[0-9a-f]{6}$/)
  })

  test('a user-assigned layer colour wins over the fallback', () => {
    addFinding(finding('f1', { layer: 'l1' }))
    const layers = new Map(useGraphStore.getState().layers)
    layers.set('l1', { visible: true, color: '#ff0000' })
    useGraphStore.setState({ layers })

    render(<FindingsPanel />)

    expect(screen.getByTestId('layer-dot').getAttribute('data-color')).toBe('#ff0000')
  })

  test('a finding without a layer gets no colour', () => {
    addFinding(finding('f1', { layer: null }))

    render(<FindingsPanel />)

    expect(screen.getByTestId('layer-dot').getAttribute('data-color')).toBe('')
  })

  test('newest gold first ([23-D] handoff reads recent findings first)', () => {
    addFinding(finding('old', { title: 'old', created_at: '2026-07-01T00:00:00+00:00' }))
    addFinding(finding('new', { title: 'new', created_at: '2026-07-09T00:00:00+00:00' }))

    render(<FindingsPanel />)

    const titles = screen.getAllByTestId('finding-item').map((li) => li.textContent)
    expect(titles[0]).toContain('new')
  })
})

describe('click → detail + focus', () => {
  test('opens the detail with body and evidence', async () => {
    addFinding(
      finding('f1', {
        body: '상세 근거 서술',
        evidence: ['https://example.test/doc'],
        node_ids: ['a'],
      }),
    )
    render(<FindingsPanel />)

    await userEvent.click(screen.getByRole('button', { expanded: false }))

    expect(screen.getByTestId('finding-body')).toHaveTextContent('상세 근거 서술')
    expect(within(screen.getByTestId('finding-evidence')).getByText('https://example.test/doc')).toBeInTheDocument()
  })

  test('clicking focuses the first anchor (store) and calls the view seam', async () => {
    push({ op: 'graph.batch', seq: ++seq, data: { nodes_added: [node('a')], nodes_updated: [], nodes_deleted: [], edges_added: [], edges_updated: [], edges_deleted: [] } })
    addFinding(finding('f1', { node_ids: ['a', 'b'] }))
    const onFocusFinding = vi.fn()

    render(<FindingsPanel onFocusFinding={onFocusFinding} />)
    await userEvent.click(screen.getByRole('button', { expanded: false }))

    expect(useGraphStore.getState().focus).toBe('a')
    // Every anchor is handed over — the finding points at a subgraph, and the
    // overview highlights all of it ([23-F] TASK 7).
    expect(onFocusFinding).toHaveBeenCalledWith(['a', 'b'])
  })

  test('a finding with no anchors opens without touching focus', async () => {
    addFinding(finding('f1', { node_ids: [] }))
    const onFocusFinding = vi.fn()

    render(<FindingsPanel onFocusFinding={onFocusFinding} />)
    await userEvent.click(screen.getByRole('button', { expanded: false }))

    expect(screen.getByTestId('finding-detail')).toBeInTheDocument()
    expect(useGraphStore.getState().focus).toBeNull()
    expect(onFocusFinding).not.toHaveBeenCalled()
  })

  test('clicking again collapses the detail', async () => {
    addFinding(finding('f1', { body: 'x' }))
    render(<FindingsPanel />)

    await userEvent.click(screen.getByRole('button', { expanded: false }))
    expect(screen.getByTestId('finding-detail')).toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { expanded: true }))
    expect(screen.queryByTestId('finding-detail')).not.toBeInTheDocument()
  })
})

describe('min_confidence filter', () => {
  test('filters out findings below the threshold', async () => {
    addFinding(finding('low', { title: 'low', confidence: 0.2 }))
    addFinding(finding('high', { title: 'high', confidence: 0.9 }))
    render(<FindingsPanel />)

    expect(screen.getAllByTestId('finding-item')).toHaveLength(2)

    await userEvent.selectOptions(screen.getByLabelText('min confidence'), '0.5')

    const items = screen.getAllByTestId('finding-item')
    expect(items).toHaveLength(1)
    expect(items[0]).toHaveTextContent('high')
  })

  test('the threshold is inclusive', async () => {
    addFinding(finding('exact', { title: 'exact', confidence: 0.5 }))
    render(<FindingsPanel />)

    await userEvent.selectOptions(screen.getByLabelText('min confidence'), '0.5')

    expect(screen.getAllByTestId('finding-item')).toHaveLength(1)
  })
})

describe('[11] untrusted strings', () => {
  test('a script tag in a title renders as text, not markup', () => {
    addFinding(finding('f1', { title: '<script>alert(1)</script>' }))

    const { container } = render(<FindingsPanel />)

    expect(screen.getByText('<script>alert(1)</script>')).toBeInTheDocument()
    expect(container.querySelector('script')).toBeNull()
    expect(container.innerHTML).toContain('&lt;script&gt;')
  })

  test('a script tag in a body renders as text', async () => {
    addFinding(finding('f1', { body: '<img src=x onerror=alert(1)>' }))
    const { container } = render(<FindingsPanel />)

    await userEvent.click(screen.getByRole('button', { expanded: false }))

    expect(screen.getByTestId('finding-body')).toHaveTextContent('<img src=x onerror=alert(1)>')
    expect(container.querySelector('img')).toBeNull()
  })

  test('http evidence is a real link', async () => {
    addFinding(finding('f1', { evidence: ['https://example.test/doc'] }))
    render(<FindingsPanel />)

    await userEvent.click(screen.getByRole('button', { expanded: false }))

    const link = screen.getByRole('link', { name: 'https://example.test/doc' })
    expect(link).toHaveAttribute('href', 'https://example.test/doc')
    expect(link).toHaveAttribute('rel', expect.stringContaining('noopener'))
  })

  test.each([
    ['javascript:alert(1)'],
    ['data:text/html,<script>alert(1)</script>'],
    ['vbscript:msgbox(1)'],
    ['trace://0x1400'],
    ['C:/reports/trace.txt'],
    ['file:///etc/passwd'],
  ])('non-http evidence %s is plain text, never an anchor ([11] allowlist)', async (url) => {
    addFinding(finding('f1', { evidence: [url] }))
    render(<FindingsPanel />)

    await userEvent.click(screen.getByRole('button', { expanded: false }))

    expect(screen.getByText(url)).toBeInTheDocument()
    expect(screen.queryByRole('link')).not.toBeInTheDocument()
  })
})

describe('[24-C] finding 이력 — gold 의 이전 판본', () => {
  const superseded = [
    { prev: { body: '이전 판단: 인증은 서버에서만' }, at: '2026-01-01T00:00:00Z', by: 'claude' },
  ]

  test('the previous version is shown in the detail', async () => {
    addFinding(finding('f1', { body: '최신 판단', _superseded: superseded }))
    render(<FindingsPanel />)

    await userEvent.click(screen.getByRole('button', { expanded: false }))

    const section = screen.getByTestId('superseded-section')
    expect(section).toHaveTextContent('이전 판단: 인증은 서버에서만')
    expect(section).toHaveTextContent('claude')
  })

  test('the correction log is shown in the detail', async () => {
    addFinding(
      finding('f1', { _provenance: [{ action: 'correction', at: '2026-01-02T00:00:00Z', by: null }] }),
    )
    render(<FindingsPanel />)

    await userEvent.click(screen.getByRole('button', { expanded: false }))

    expect(screen.getByTestId('provenance-action')).toHaveTextContent('correction')
  })

  test('a finding without history shows neither section', async () => {
    addFinding(finding('f1', { body: 'b' }))
    render(<FindingsPanel />)

    await userEvent.click(screen.getByRole('button', { expanded: false }))

    expect(screen.queryByTestId('superseded-section')).not.toBeInTheDocument()
    expect(screen.queryByTestId('provenance-section')).not.toBeInTheDocument()
  })

  test('[8-C] history arriving by finding.update reaches the panel', async () => {
    addFinding(finding('f1', { body: 'old' }))
    push({
      op: 'finding.update',
      seq: ++seq,
      data: {
        finding_id: 'f1',
        patch: { set: { body: 'new', _superseded: superseded } },
      },
    })
    render(<FindingsPanel />)

    await userEvent.click(screen.getByRole('button', { expanded: false }))

    expect(screen.getByTestId('finding-body')).toHaveTextContent('new')
    expect(screen.getByTestId('superseded-section')).toHaveTextContent(
      '이전 판단: 인증은 서버에서만',
    )
  })

  test('[11] an archived finding body renders as text, never as markup', async () => {
    addFinding(
      finding('f1', {
        _superseded: [{ prev: { body: '<img src=x onerror="alert(1)">' }, at: 't', by: null }],
      }),
    )
    const { container } = render(<FindingsPanel />)

    await userEvent.click(screen.getByRole('button', { expanded: false }))

    expect(container.querySelector('img')).toBeNull()
    expect(screen.getByTestId('superseded-value')).toHaveTextContent(
      '<img src=x onerror="alert(1)">',
    )
  })
})
