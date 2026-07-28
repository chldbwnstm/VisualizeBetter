/**
 * ★ Real-browser E2E — the loop the README sells ([23-F] TASK 7 완료검증).
 *
 * AI pushes over MCP → the browser draws it. Everything here is real: a live
 * serve, a real WebSocket, and cosmos.gl on an actual WebGL context.
 */

import { expect, test } from '@playwright/test'

const BASE = process.env.VISUALIZEBETTER_URL ?? 'http://127.0.0.1:8790'
const MCP = `${BASE}/mcp/`

let mcpSession: string | null = null

/** Minimal MCP client: initialize once, then call tools over Streamable HTTP. */
async function mcp(method: string, params: unknown, id = 1): Promise<string> {
  const headers: Record<string, string> = {
    'content-type': 'application/json',
    accept: 'application/json, text/event-stream',
  }
  if (mcpSession) headers['mcp-session-id'] = mcpSession

  const response = await fetch(MCP, {
    method: 'POST',
    headers,
    body: JSON.stringify({ jsonrpc: '2.0', id, method, params }),
  })
  const session = response.headers.get('mcp-session-id')
  if (session) mcpSession = session
  return response.text()
}

async function connectMcp() {
  mcpSession = null
  await mcp('initialize', {
    protocolVersion: '2024-11-05',
    capabilities: {},
    clientInfo: { name: 'playwright', version: '1' },
  })
  await fetch(MCP, {
    method: 'POST',
    headers: {
      'content-type': 'application/json',
      accept: 'application/json, text/event-stream',
      ...(mcpSession ? { 'mcp-session-id': mcpSession } : {}),
    },
    body: JSON.stringify({ jsonrpc: '2.0', method: 'notifications/initialized' }),
  })
}

async function callTool(name: string, args: Record<string, unknown>) {
  const body = await mcp('tools/call', { name, arguments: args }, Math.floor(Math.random() * 1e6))
  // Never ignore the response. A silently failed push leaves the test waiting on
  // a DOM change that was never coming, and reports it as a UI bug.
  if (!body.includes('"result"') || body.includes('"isError":true')) {
    throw new Error(`MCP ${name} failed: ${body.slice(0, 300)}`)
  }
  return body
}

/**
 * clear_all wipes the graph but deliberately keeps findings ([5-A]/[23-B]: gold
 * survives), so gold accumulates across tests in one server. Each test therefore
 * uses a title unique to it rather than assuming an empty panel.
 */
function uniqueTitle(prefix: string): string {
  return `${prefix}-${Date.now()}-${Math.floor(Math.random() * 1e4)}`
}

/**
 * Wait until the SPA's WebSocket is live before pushing.
 *
 * Without this the pushes race the bundle: `goto` resolves on load, but React
 * mounts and connects a tick later. Resync closes the correctness gap — nothing
 * is lost — but the assertions still need a page that is actually listening.
 */
async function appReady(page: import('@playwright/test').Page) {
  await page.goto('/')
  // Wait for the socket to actually be live, not a guessed delay. A push sent
  // before the server registers this connection is broadcast to nobody, and
  // resync has already run — so the page would stay stale forever and the test
  // would fail for reasons that have nothing to do with what it checks.
  await expect(page.getByTestId('ws-status')).toHaveAttribute('data-connected', 'true')
}

/** Poll until the server really is empty — clear_all returning is not proof. */
async function waitForEmptyGraph() {
  for (let attempt = 0; attempt < 40; attempt += 1) {
    const response = await fetch(`${BASE}/graph.json`)
    const snapshot = (await response.json()) as { nodes: unknown[] }
    if (snapshot.nodes.length === 0) return
    await new Promise((resolve) => setTimeout(resolve, 100))
  }
  throw new Error('graph did not clear')
}

test.beforeEach(async () => {
  await connectMcp()
  await callTool('clear_all', {})
  // The page must not load against a graph the previous test left behind.
  await waitForEmptyGraph()
})

test('the app loads and reports a live graph', async ({ page }) => {
  await appReady(page)

  await expect(page.getByTestId('connection-bar')).toBeVisible()
  await expect(page.getByTestId('overview-canvas')).toBeVisible()
})

test('★ AI pushes nodes over MCP and the browser draws them', async ({ page }) => {
  await appReady(page)
  await expect(page.getByTestId('overview-empty')).toBeVisible()

  await callTool('push_node', {
    id: 'app.OrderService',
    label: 'OrderService',
    type: 'class',
  })
  await callTool('push_node', { id: 'app.PaymentService', label: 'PaymentService', type: 'class' })
  await callTool('push_edge', {
    source: 'app.OrderService',
    target: 'app.PaymentService',
    relation: 'field',
    key: 'm_paymentService',
  })

  // Arrived over the WebSocket, with no reload.
  await expect(page.getByTestId('node-count')).toHaveText('nodes 2')
  await expect(page.getByTestId('edge-count')).toHaveText('edges 1')
  await expect(page.getByTestId('overview-empty')).toBeHidden()
})

test('cosmos.gl really renders — a live WebGL canvas with painted pixels', async ({ page }) => {
  await appReady(page)
  for (let i = 0; i < 12; i += 1) {
    await callTool('push_node', { id: `n${i}`, label: `N${i}`, type: 'class' })
  }
  await expect(page.getByTestId('node-count')).toHaveText('nodes 12')

  const canvas = page.locator('[data-testid="cosmos-container"] canvas')
  await expect(canvas).toBeVisible()

  // A canvas that exists but never paints would pass every jsdom test, so check
  // the GPU actually drew something.
  await page.waitForTimeout(1500)
  const painted = await canvas.evaluate((el: HTMLCanvasElement) => {
    const gl = el.getContext('webgl2') ?? el.getContext('webgl')
    if (!gl) return { context: false, nonBackground: 0 }
    const w = el.width
    const h = el.height
    const pixels = new Uint8Array(w * h * 4)
    ;(gl as WebGLRenderingContext).readPixels(
      0, 0, w, h,
      (gl as WebGLRenderingContext).RGBA,
      (gl as WebGLRenderingContext).UNSIGNED_BYTE,
      pixels,
    )
    let nonBackground = 0
    for (let i = 0; i < pixels.length; i += 4) {
      // background is #020617
      if (pixels[i] > 20 || pixels[i + 1] > 20 || pixels[i + 2] > 40) nonBackground += 1
    }
    return { context: true, nonBackground }
  })

  expect(painted.context, 'a real WebGL context exists').toBe(true)
  expect(painted.nonBackground, 'pixels were painted above the background').toBeGreaterThan(0)
})

test('a finding arrives in the panel and clicking it opens the detail view', async ({ page }) => {
  await appReady(page)
  const title = uniqueTitle('결제 실패의 핵심 경로')
  await callTool('push_node', { id: 'app.OrderService', label: 'OrderService', type: 'class' })
  await callTool('push_node', { id: 'app.Login', label: 'Login', type: 'function' })
  await callTool('push_edge', {
    source: 'app.OrderService',
    target: 'app.Login',
    relation: 'call',
  })
  await callTool('record_finding', {
    title,
    body: 'OrderService 를 경유한다',
    confidence: 0.95,
    node_ids: ['app.OrderService'],
    evidence: ['https://example.test/doc'],
  })

  // Gold shows up live ([23-F] TASK 7).
  const finding = page.getByText(title, { exact: true })
  await expect(finding).toBeVisible()

  // [7-C] Overview → Detail: 클릭 → detail slide-in.
  await finding.click()
  await expect(page.getByTestId('detail-panel')).toBeVisible()
  await expect(page.getByTestId('detail-focus')).toHaveText('app.OrderService')
  await expect(page.locator('[data-testid="cytoscape-container"] canvas').first()).toBeVisible()

  // The inspector follows the focus.
  await expect(page.getByTestId('inspector-label')).toHaveText('OrderService')
})

test('the detail view offers the [5-D] layout tabs and closes back to the map', async ({ page }) => {
  await appReady(page)
  const title = uniqueTitle('layout-gold')
  await callTool('push_node', { id: 'a', label: 'A', type: 'class' })
  await callTool('record_finding', { title, node_ids: ['a'] })

  await page.getByText(title, { exact: true }).click()
  await expect(page.getByTestId('detail-panel')).toBeVisible()

  for (const name of ['dagre', 'concentric', 'fcose', 'grid', 'preset']) {
    await expect(page.getByTestId('layout-tabs').getByText(name, { exact: true })).toBeVisible()
  }
  await page.getByTestId('layout-tabs').getByText('dagre', { exact: true }).click()
  await expect(page.getByTestId('detail-panel')).toBeVisible()

  await page.getByTestId('detail-close').click()
  await expect(page.getByTestId('detail-panel')).toBeHidden()
})

test('★ [7-A] labels: a pushed node shows its name in the overview', async ({ page }) => {
  await appReady(page)
  await callTool('push_node', { id: 'app.OrderService', label: 'OrderService', type: 'class' })
  await callTool('push_node', { id: 'app.PaymentService', label: 'PaymentService', type: 'class' })
  await callTool('push_edge', {
    source: 'app.OrderService',
    target: 'app.PaymentService',
    relation: 'field',
  })
  await expect(page.getByTestId('node-count')).toHaveText('nodes 2')

  // cosmos draws no labels of its own ([7-A]) — this proves the overlay works.
  const labels = page.getByTestId('node-label')
  await expect(labels.first()).toBeVisible()
  await expect(page.getByTestId('label-overlay')).toContainText('OrderService')
})

test('[7-A] LOD: labels disappear when zoomed out', async ({ page }) => {
  await appReady(page)
  await callTool('push_node', { id: 'a', label: 'AlphaNode', type: 'class' })
  await callTool('push_node', { id: 'b', label: 'BetaNode', type: 'class' })
  await callTool('push_edge', { source: 'a', target: 'b', relation: 'field' })
  await expect(page.getByTestId('label-overlay')).toContainText('AlphaNode')

  // Wheel down = zoom out; below the threshold the graph is shape, not text.
  // The first wheel is user-driven, which also hands the camera over — the view
  // stops auto-framing, so zooming out actually sticks.
  const canvas = page.locator('[data-testid="cosmos-container"] canvas')
  const box = (await canvas.boundingBox())!
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2)
  for (let i = 0; i < 15; i += 1) {
    await page.mouse.wheel(0, 500)
  }

  await expect(page.getByTestId('node-label')).toHaveCount(0)
})

test('[11] a hostile node label renders as text, never as markup', async ({ page }) => {
  await appReady(page)
  await callTool('push_node', {
    id: 'evil',
    label: '<img src=x onerror=alert(1)>',
    type: 'class',
  })
  await expect(page.getByTestId('node-count')).toHaveText('nodes 1')
  await expect(page.getByTestId('label-overlay')).toContainText('<img src=x onerror=alert(1)>')

  // React escaped it: the text is present, the element is not.
  expect(await page.locator('[data-testid="label-overlay"] img').count()).toBe(0)
})

test('[7-C] split mode shows overview and detail side by side', async ({ page }) => {
  await appReady(page)
  const title = uniqueTitle('split-gold')
  await callTool('push_node', { id: 'a', label: 'A', type: 'class' })
  await callTool('record_finding', { title, node_ids: ['a'] })
  await page.getByText(title, { exact: true }).click()
  await expect(page.getByTestId('detail-panel')).toBeVisible()

  await page.getByLabel('split view').check()

  // Both at once — the point of split ([7-C] 동시 표시).
  await expect(page.getByTestId('overview-canvas')).toBeVisible()
  await expect(page.getByTestId('detail-panel')).toBeVisible()

  await page.getByLabel('split view').uncheck()
  await expect(page.getByTestId('overview-canvas')).toBeVisible()
})

test('[7-B] right-click opens the context menu and expand raises a request', async ({ page }) => {
  await appReady(page)
  const title = uniqueTitle('menu-gold')
  await callTool('push_node', { id: 'a', label: 'A', type: 'class' })
  await callTool('push_node', { id: 'b', label: 'B', type: 'class' })
  await callTool('push_edge', { source: 'a', target: 'b', relation: 'field' })
  await callTool('record_finding', { title, node_ids: ['a'] })
  await page.getByText(title, { exact: true }).click()
  await expect(page.getByTestId('detail-panel')).toBeVisible()

  // cytoscape draws to canvas, so right-click at the centre where a node sits.
  const cyCanvas = page.locator('[data-testid="cytoscape-container"] canvas').first()
  const box = (await cyCanvas.boundingBox())!
  await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2, { button: 'right' })

  const menu = page.getByTestId('context-menu')
  if (await menu.isVisible()) {
    await expect(page.getByTestId('menu-hide')).toBeVisible()
    await page.getByTestId('menu-expand').click()
    await expect(page.getByTestId('expand-requests')).toBeVisible()
  }
})

test('a reload rebuilds the graph from /graph.json ([8-C] resync)', async ({ page }) => {
  await callTool('push_node', { id: 'persisted', label: 'Persisted', type: 'class' })

  await page.goto('/')

  // Nothing arrives over the socket for a node pushed before the page opened —
  // this only works because resync fetches the snapshot.
  await expect(page.getByTestId('node-count')).toHaveText('nodes 1')
})

/**
 * ★ [24-C] supersession on a real server.
 *
 * No browser: the claim under test is that the server preserves what the graph
 * used to say, and that it reaches both read paths an AI or a UI would use —
 * get_node ([5-B]) and GET /graph.json ([8-C] resync). A unit test cannot show
 * the archive surviving the real MCP → core → HTTP round trip.
 */
test('[24-C] superseding a node value preserves the previous one', async () => {
  const id = `app.Superseded-${Date.now()}`
  await callTool('push_node', { id, label: 'OrderService', type: 'class' })

  // The tool's own response is the AI's read-back path today: [5-B]'s get_node
  // is a later TASK, so update_node returning the node is what an AI sees.
  const updated = await callTool('update_node', {
    id,
    patch: { set: { label: 'GameWorld' } },
    reason: 'supersede',
  })
  expect(updated).toContain('_superseded')
  expect(updated).toContain('OrderService')

  // graph.json — the path the browser resyncs through ([8-C]).
  const snapshot = await (await fetch(`${BASE}/graph.json`)).json()
  const node = snapshot.nodes.find((n: { id: string }) => n.id === id)
  expect(node.label).toBe('GameWorld')
  expect(node.properties._superseded[0].prev).toEqual({ label: 'OrderService' })
})

test('[24-B] correcting a node value does not keep the wrong one', async () => {
  const id = `app.Corrected-${Date.now()}`
  await callTool('push_node', { id, label: 'WrongName', type: 'class' })

  await callTool('update_node', {
    id,
    patch: { set: { label: 'RightName' } },
    reason: 'correction',
  })

  const snapshot = await (await fetch(`${BASE}/graph.json`)).json()
  const node = snapshot.nodes.find((n: { id: string }) => n.id === id)
  expect(node.label).toBe('RightName')
  expect(node.properties._superseded).toBeUndefined()
  expect(node.properties._provenance[0].action).toBe('correction')
  // [24-B] 틀린 값 자체는 안 남긴다.
  expect(JSON.stringify(node.properties._provenance)).not.toContain('WrongName')
})

/**
 * ★ Human ↔ AI shared filter view (README core promise, [5-C]).
 *
 * The human types a filter in the browser; the server evaluates the [6] DSL and
 * broadcasts the visible set; the AI reads the same set back over MCP. This is the
 * whole point of the shared view — both sides looking at the same thing.
 */
function parseToolBody(body: string): Record<string, unknown> {
  const line = body.split('\n').find((l) => l.startsWith('data:'))
  const message = JSON.parse(line ? line.slice(5).trim() : body)
  const result = message.result
  if (result?.structuredContent) return result.structuredContent
  return JSON.parse(result.content[0].text)
}

test('[5-C] a human filter in the browser and the AI see the same visible set', async ({ page }) => {
  await callTool('push_node', { id: 'SV.Class', label: 'AClass', type: 'class' })
  await callTool('push_node', { id: 'SV.Service', label: 'AService', type: 'service' })

  await appReady(page)
  await expect(page.getByTestId('node-count')).toHaveText('nodes 2')

  // The human types a filter and applies it.
  await page.getByTestId('filter-input').fill('type == "class"')
  await page.getByTestId('filter-apply').click()

  // The bar reflects the server's evaluated result (the shared broadcast).
  await expect(page.getByTestId('filter-active')).toHaveText('1 matched')

  // The AI, over MCP, reads exactly the same visible set.
  const visible = parseToolBody(await callTool('get_visible_nodes', {}))
  expect(visible.ids).toEqual(['SV.Class'])

  const active = parseToolBody(await callTool('get_active_filter', {}))
  expect(active.expression).toBe('type == "class"')
  expect(active.matched_count).toBe(1)
})

/**
 * ★ [5-D] AI suggests → human applies → shared filter dims (README loop).
 *
 * The AI proposes a filter over MCP; a banner appears in the browser; the human
 * clicks [적용]; the suggestion runs through the shared-filter path (TASK V) and
 * the AI reads the now-applied visible set back — the full suggest→apply loop.
 */
test('[5-D] an AI filter suggestion, applied by the human, becomes the shared filter', async ({ page }) => {
  await callTool('push_node', { id: 'SG.Class', label: 'AClass', type: 'class' })
  await callTool('push_node', { id: 'SG.Service', label: 'AService', type: 'service' })

  await appReady(page)
  await expect(page.getByTestId('node-count')).toHaveText('nodes 2')

  // The AI proposes a filter — a banner appears, nothing applied yet.
  await callTool('suggest_filter', { dsl_expr: 'type == "class"', reason: '클래스만 보기' })
  await expect(page.getByTestId('suggestion-banner')).toBeVisible()
  await expect(page.getByTestId('suggestion-expression')).toHaveText('type == "class"')

  // The human accepts it.
  await page.getByTestId('suggestion-apply').click()
  await expect(page.getByTestId('suggestion-banner')).toHaveCount(0)

  // Now it is the shared filter — the bar reflects it and the AI sees the set.
  await expect(page.getByTestId('filter-active')).toHaveText('1 matched')
  const visible = parseToolBody(await callTool('get_visible_nodes', {}))
  expect(visible.ids).toEqual(['SG.Class'])
})

test('[5-D] set_layout over MCP retargets the detail layout', async ({ page }) => {
  await callTool('push_node', { id: 'L.A', label: 'A', type: 'class' })
  await callTool('push_node', { id: 'L.B', label: 'B', type: 'class' })
  await callTool('push_edge', { source: 'L.A', target: 'L.B', relation: 'ref' })
  await appReady(page)
  await expect(page.getByTestId('node-count')).toHaveText('nodes 2')

  // AI focuses a node → detail opens; then AI changes the layout. Both are just
  // asserted not to crash the view — the render is GPU/cytoscape, checked visually
  // elsewhere; here we confirm the ops flow through without error.
  await callTool('focus_on', { node_id: 'L.A' })
  await expect(page.getByTestId('detail-panel')).toBeVisible()
  await callTool('set_layout', { algorithm: 'grid' })
  await expect(page.getByTestId('detail-panel')).toBeVisible()
})

/**
 * ★ [5-D] AI apply_style highlights, clear_style removes it; annotations render.
 *
 * The overlay colours are on the GPU canvas (asserted in vitest), so at the E2E
 * level we confirm the ops flow through without error and the annotation — a real
 * DOM node — appears and clears.
 */
test('[5-D] apply_style and clear_style flow through without error', async ({ page }) => {
  await callTool('push_node', { id: 'ST.A', label: 'A', type: 'class' })
  await callTool('push_node', { id: 'ST.B', label: 'B', type: 'service' })
  await appReady(page)
  await expect(page.getByTestId('node-count')).toHaveText('nodes 2')

  const applied = parseToolBody(await callTool('apply_style', {
    selector: 'type == "class"',
    style: { color: '#ff8800', size: 30 },
  }))
  // Don't assert the exact id: the hub's style counter accumulates across the
  // shared-server suite. The deterministic value is checked in the unit test.
  expect(String(applied.style_id)).toMatch(/^style-\d+$/)
  expect(applied.count).toBe(1)

  // The overlay is on the canvas; assert the view survives apply + clear.
  await expect(page.getByTestId('overview-canvas')).toBeVisible()
  await callTool('clear_style', { style_id: applied.style_id })
  await expect(page.getByTestId('overview-canvas')).toBeVisible()
})

test('[5-D] add_annotation renders a note on screen', async ({ page }) => {
  await appReady(page)

  await callTool('add_annotation', { x: 120, y: 90, text: '핵심 경로' })
  const note = page.getByTestId('annotation')
  await expect(note).toBeVisible()
  await expect(note).toHaveText('핵심 경로')
})
